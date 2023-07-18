"""Miscellaneous type operations and helpers for use during type checking.

NOTE: These must not be accessed from mypy.nodes or mypy.types to avoid import
      cycles. These must not be called from the semantic analysis main pass
      since these may assume that MROs are ready.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Any, Callable, Iterable, List, Sequence, TypeVar, cast

from mypy.copytype import copy_type
from mypy.expandtype import expand_type, expand_type_by_instance
from mypy.maptype import map_instance_to_supertype
from mypy.nodes import (
    ARG_POS,
    ARG_STAR,
    ARG_STAR2,
    SYMBOL_FUNCBASE_TYPES,
    Decorator,
    Expression,
    FuncBase,
    FuncDef,
    FuncItem,
    OverloadedFuncDef,
    OverloadPart,
    StrExpr,
    TypeInfo,
    Var,
)
from mypy.state import state
from mypy.types import (
    ENUM_REMOVED_PROPS,
    AnyType,
    CallableType,
    ExtraAttrs,
    FormalArgument,
    FunctionLike,
    Instance,
    IntersectionType,
    LiteralType,
    NoneType,
    Overloaded,
    Parameters,
    ParamSpecType,
    PartialType,
    ProperType,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeOfAny,
    TypeQuery,
    TypeType,
    TypeVarLikeType,
    TypeVarTupleType,
    TypeVarType,
    UninhabitedType,
    UnionType,
    UnpackType,
    UntypedType,
    flatten_nested_unions,
    get_proper_type,
    get_proper_types,
    is_unannotated_any,
)
from mypy.typevars import fill_typevars


def is_recursive_pair(s: Type, t: Type) -> bool:
    """Is this a pair of recursive types?

    There may be more cases, and we may be forced to use e.g. has_recursive_types()
    here, but this function is called in very hot code, so we try to keep it simple
    and return True only in cases we know may have problems.
    """
    if isinstance(s, TypeAliasType) and s.is_recursive:
        return (
            isinstance(get_proper_type(t), (Instance, UnionType))
            or isinstance(t, TypeAliasType)
            and t.is_recursive
            # Tuple types are special, they can cause an infinite recursion even if
            # the other type is not recursive, because of the tuple fallback that is
            # calculated "on the fly".
            or isinstance(get_proper_type(s), TupleType)
        )
    if isinstance(t, TypeAliasType) and t.is_recursive:
        return (
            isinstance(get_proper_type(s), (Instance, UnionType))
            or isinstance(s, TypeAliasType)
            and s.is_recursive
            # Same as above.
            or isinstance(get_proper_type(t), TupleType)
        )
    return False


def tuple_fallback(typ: TupleType) -> Instance:
    """Return fallback type for a tuple."""
    from mypy.join import join_type_list

    info = typ.partial_fallback.type
    if info.fullname != "builtins.tuple":
        return typ.partial_fallback
    items = []
    for item in typ.items:
        if isinstance(item, UnpackType):
            unpacked_type = get_proper_type(item.type)
            if isinstance(unpacked_type, TypeVarTupleType):
                items.append(unpacked_type.upper_bound)
            elif isinstance(unpacked_type, TupleType):
                # TODO: might make sense to do recursion here to support nested unpacks
                # of tuple constants
                items.extend(unpacked_type.items)
            elif (
                isinstance(unpacked_type, Instance)
                and unpacked_type.type.fullname == "builtins.tuple"
            ):
                items.append(unpacked_type.args[0])
            else:
                raise NotImplementedError
        else:
            items.append(item)
    return Instance(info, [join_type_list(items)], extra_attrs=typ.partial_fallback.extra_attrs)


def get_self_type(func: CallableType, default_self: Instance | TupleType) -> Type | None:
    if isinstance(get_proper_type(func.ret_type), UninhabitedType):
        return func.ret_type
    elif func.arg_types and func.arg_types[0] != default_self and func.arg_kinds[0] == ARG_POS:
        return func.arg_types[0]
    else:
        return None


def type_object_type_from_function(
    signature: FunctionLike, info: TypeInfo, def_info: TypeInfo, fallback: Instance, is_new: bool
) -> FunctionLike:
    # We first need to record all non-trivial (explicit) self types in __init__,
    # since they will not be available after we bind them. Note, we use explicit
    # self-types only in the defining class, similar to __new__ (but not exactly the same,
    # see comment in class_callable below). This is mostly useful for annotating library
    # classes such as subprocess.Popen.
    default_self = fill_typevars(info)
    if not is_new and not info.is_newtype:
        orig_self_types = [get_self_type(it, default_self) for it in signature.items]
    else:
        orig_self_types = [None] * len(signature.items)

    # The __init__ method might come from a generic superclass 'def_info'
    # with type variables that do not map identically to the type variables of
    # the class 'info' being constructed. For example:
    #
    #   class A(Generic[T]):
    #       def __init__(self, x: T) -> None: ...
    #   class B(A[List[T]]):
    #      ...
    #
    # We need to map B's __init__ to the type (List[T]) -> None.
    signature = bind_self(signature, original_type=default_self, is_classmethod=is_new)
    signature = cast(FunctionLike, map_type_from_supertype(signature, info, def_info))

    special_sig: str | None = None
    if def_info.fullname == "builtins.dict":
        # Special signature!
        special_sig = "dict"

    if isinstance(signature, CallableType):
        return class_callable(signature, info, fallback, special_sig, is_new, orig_self_types[0])
    else:
        # Overloaded __init__/__new__.
        assert isinstance(signature, Overloaded)
        items: list[CallableType] = []
        for item, orig_self in zip(signature.items, orig_self_types):
            items.append(class_callable(item, info, fallback, special_sig, is_new, orig_self))
        return Overloaded(items)


def class_callable(
    init_type: CallableType,
    info: TypeInfo,
    type_type: Instance,
    special_sig: str | None,
    is_new: bool,
    orig_self_type: Type | None = None,
) -> CallableType:
    """Create a type object type based on the signature of __init__."""
    variables: list[TypeVarLikeType] = []
    variables.extend(info.defn.type_vars)
    variables.extend(init_type.variables)

    from mypy.subtypes import is_subtype

    init_ret_type = get_proper_type(init_type.ret_type)
    orig_self_type = get_proper_type(orig_self_type)
    default_ret_type = fill_typevars(info)
    explicit_type = init_ret_type if is_new else orig_self_type
    if (
        isinstance(explicit_type, (Instance, TupleType, UninhabitedType))
        # We have to skip protocols, because it can be a subtype of a return type
        # by accident. Like `Hashable` is a subtype of `object`. See #11799
        and isinstance(default_ret_type, Instance)
        and not default_ret_type.type.is_protocol
        # Only use the declared return type from __new__ or declared self in __init__
        # if it is actually returning a subtype of what we would return otherwise.
        and is_subtype(explicit_type, default_ret_type, ignore_type_params=True)
    ):
        ret_type: Type = explicit_type
    else:
        ret_type = default_ret_type

    callable_type = init_type.copy_modified(
        ret_type=ret_type,
        fallback=type_type,
        name=None,
        variables=variables,
        special_sig=special_sig,
    )
    c = callable_type.with_name(info.name)
    return c


def map_type_from_supertype(typ: Type, sub_info: TypeInfo, super_info: TypeInfo) -> Type:
    """Map type variables in a type defined in a supertype context to be valid
    in the subtype context. Assume that the result is unique; if more than
    one type is possible, return one of the alternatives.

    For example, assume

      class D(Generic[S]): ...
      class C(D[E[T]], Generic[T]): ...

    Now S in the context of D would be mapped to E[T] in the context of C.
    """
    # Create the type of self in subtype, of form t[a1, ...].
    inst_type = fill_typevars(sub_info)
    if isinstance(inst_type, TupleType):
        inst_type = tuple_fallback(inst_type)
    # Map the type of self to supertype. This gets us a description of the
    # supertype type variables in terms of subtype variables, i.e. t[t1, ...]
    # so that any type variables in tN are to be interpreted in subtype
    # context.
    inst_type = map_instance_to_supertype(inst_type, super_info)
    # Finally expand the type variables in type with those in the previously
    # constructed type. Note that both type and inst_type may have type
    # variables, but in type they are interpreted in supertype context while
    # in inst_type they are interpreted in subtype context. This works even if
    # the names of type variables in supertype and subtype overlap.
    return expand_type_by_instance(typ, inst_type)


def supported_self_type(typ: ProperType) -> bool:
    """Is this a supported kind of explicit self-types?

    Currently, this means a X or Type[X], where X is an instance or
    a type variable with an instance upper bound.
    """
    if isinstance(typ, TypeType):
        return supported_self_type(typ.item)
    return isinstance(typ, TypeVarType) or (
        isinstance(typ, Instance) and typ != fill_typevars(typ.type)
    )


F = TypeVar("F", bound=FunctionLike)


def bind_self(method: F, original_type: Type | None = None, is_classmethod: bool = False) -> F:
    """Return a copy of `method`, with the type of its first parameter (usually
    self or cls) bound to original_type.

    If the type of `self` is a generic type (T, or Type[T] for classmethods),
    instantiate every occurrence of type with original_type in the rest of the
    signature and in the return type.

    original_type is the type of E in the expression E.copy(). It is None in
    compatibility checks. In this case we treat it as the erasure of the
    declared type of self.

    This way we can express "the type of self". For example:

    T = TypeVar('T', bound='A')
    class A:
        def copy(self: T) -> T: ...

    class B(A): pass

    b = B().copy()  # type: B

    """
    if isinstance(method, Overloaded):
        return cast(
            F, Overloaded([bind_self(c, original_type, is_classmethod) for c in method.items])
        )
    assert isinstance(method, CallableType)
    func = method
    if not func.arg_types:
        # Invalid method, return something.
        return cast(F, func)
    if func.arg_kinds[0] == ARG_STAR:
        # The signature is of the form 'def foo(*args, ...)'.
        # In this case we shouldn't drop the first arg,
        # since func will be absorbed by the *args.

        # TODO: infer bounds on the type of *args?
        return cast(F, func)
    self_param_type = get_proper_type(func.arg_types[0])

    variables: Sequence[TypeVarLikeType] = []
    if func.variables and supported_self_type(self_param_type):
        from mypy.infer import infer_type_arguments

        if original_type is None:
            # TODO: type check method override (see #7861).
            original_type = erase_to_bound(self_param_type)
        original_type = get_proper_type(original_type)

        all_ids = func.type_var_ids()
        typeargs = infer_type_arguments(all_ids, self_param_type, original_type, is_supertype=True)
        if (
            is_classmethod
            # TODO: why do we need the extra guards here?
            and any(isinstance(get_proper_type(t), UninhabitedType) for t in typeargs)
            and isinstance(original_type, (Instance, TypeVarType, TupleType))
        ):
            # In case we call a classmethod through an instance x, fallback to type(x)
            typeargs = infer_type_arguments(
                all_ids, self_param_type, TypeType(original_type), is_supertype=True
            )

        ids = [tid for tid in all_ids if any(tid == t.id for t in get_type_vars(self_param_type))]

        # Technically, some constrains might be unsolvable, make them <nothing>.
        to_apply = [t if t is not None else UninhabitedType() for t in typeargs]

        def expand(target: Type) -> Type:
            return expand_type(target, {id: to_apply[all_ids.index(id)] for id in ids})

        arg_types = [expand(x) for x in func.arg_types[1:]]
        ret_type = expand(func.ret_type)
        variables = [v for v in func.variables if v.id not in ids]
    else:
        arg_types = func.arg_types[1:]
        ret_type = func.ret_type
        variables = func.variables

    original_type = get_proper_type(original_type)
    if isinstance(original_type, CallableType) and original_type.is_type_obj():
        original_type = TypeType.make_normalized(original_type.ret_type)
    res = func.copy_modified(
        arg_types=arg_types,
        arg_kinds=func.arg_kinds[1:],
        arg_names=func.arg_names[1:],
        variables=variables,
        ret_type=ret_type,
        bound_args=[original_type],
    )
    return cast(F, res)


def erase_to_bound(t: Type) -> Type:
    # TODO: use value restrictions to produce a union?
    t = get_proper_type(t)
    if isinstance(t, TypeVarType):
        return t.upper_bound
    if isinstance(t, TypeType):
        if isinstance(t.item, TypeVarType):
            return TypeType.make_normalized(t.item.upper_bound)
    return t


def callable_corresponding_argument(
    typ: CallableType | Parameters, model: FormalArgument
) -> FormalArgument | None:
    """Return the argument a function that corresponds to `model`"""

    by_name = typ.argument_by_name(model.name)
    by_pos = typ.argument_by_position(model.pos)
    if by_name is None and by_pos is None:
        return None
    if by_name is not None and by_pos is not None:
        if by_name == by_pos:
            return by_name
        # If we're dealing with an optional pos-only and an optional
        # name-only arg, merge them.  This is the case for all functions
        # taking both *args and **args, or a pair of functions like so:

        # def right(a: int = ...) -> None: ...
        # def left(__a: int = ..., *, a: int = ...) -> None: ...
        from mypy.subtypes import is_equivalent

        if (
            not (by_name.required or by_pos.required)
            and by_pos.name is None
            and by_name.pos is None
            and is_equivalent(by_name.typ, by_pos.typ)
        ):
            return FormalArgument(by_name.name, by_pos.pos, by_name.typ, False)
    return by_name if by_name is not None else by_pos


def simple_literal_type(t: ProperType | None) -> Instance | None:
    """Extract the underlying fallback Instance type for a simple Literal"""
    if isinstance(t, Instance) and t.last_known_value is not None:
        t = t.last_known_value
    if isinstance(t, LiteralType):
        return t.fallback
    return None


def is_simple_literal(t: ProperType) -> bool:
    if isinstance(t, LiteralType):
        return t.fallback.type.is_enum or t.fallback.type.fullname == "builtins.str"
    if isinstance(t, Instance):
        return t.last_known_value is not None and isinstance(t.last_known_value.value, str)
    return False


def make_simplified_union(
    items: Sequence[Type],
    line: int = -1,
    column: int = -1,
    *,
    keep_erased: bool = False,
    contract_literals: bool = True,
) -> ProperType:
    """Build union type with redundant union items removed.

    If only a single item remains, this may return a non-union type.

    Examples:

    * [int, str] -> Union[int, str]
    * [int, object] -> object
    * [int, int] -> int
    * [int, Any] -> Union[int, Any] (Any types are not simplified away!)
    * [Any, Any] -> Any
    * [int, Union[bytes, str]] -> Union[int, bytes, str]

    Note: This must NOT be used during semantic analysis, since TypeInfos may not
          be fully initialized.

    The keep_erased flag is used for type inference against union types
    containing type variables. If set to True, keep all ErasedType items.

    The contract_literals flag indicates whether we need to contract literal types
    back into a sum type. Set it to False when called by try_expanding_sum_type_
    to_union().
    """
    # Step 1: expand all nested unions
    items = flatten_nested_unions(items)

    # Step 2: fast path for single item
    if len(items) == 1:
        return get_proper_type(items[0])

    # Step 3: remove redundant unions
    simplified_set: Sequence[Type] = _remove_redundant_union_items(items, keep_erased)

    # Step 4: If more than one literal exists in the union, try to simplify
    if (
        contract_literals
        and sum(isinstance(get_proper_type(item), LiteralType) for item in simplified_set) > 1
    ):
        simplified_set = try_contracting_literals_in_union(simplified_set)

    result = get_proper_type(UnionType.make_union(simplified_set, line, column))

    nitems = len(items)
    if nitems > 1 and (
        nitems > 2 or not (type(items[0]) is NoneType or type(items[1]) is NoneType)
    ):
        # Step 5: At last, we erase any (inconsistent) extra attributes on instances.

        # Initialize with None instead of an empty set as a micro-optimization. The set
        # is needed very rarely, so we try to avoid constructing it.
        extra_attrs_set: set[ExtraAttrs] | None = None
        for item in items:
            instance = try_getting_instance_fallback(item)
            if instance and instance.extra_attrs:
                if extra_attrs_set is None:
                    extra_attrs_set = {instance.extra_attrs}
                else:
                    extra_attrs_set.add(instance.extra_attrs)

        if extra_attrs_set is not None and len(extra_attrs_set) > 1:
            fallback = try_getting_instance_fallback(result)
            if fallback:
                fallback.extra_attrs = None

    return result


def _remove_redundant_union_items(items: list[Type], keep_erased: bool) -> list[Type]:
    from mypy.subtypes import is_proper_subtype

    # The first pass through this loop, we check if later items are subtypes of earlier items.
    # The second pass through this loop, we check if earlier items are subtypes of later items
    # (by reversing the remaining items)
    for _direction in range(2):
        new_items: list[Type] = []
        # seen is a map from a type to its index in new_items
        seen: dict[ProperType, int] = {}
        unduplicated_literal_fallbacks: set[Instance] | None = None
        for ti in items:
            proper_ti = get_proper_type(ti)

            # UninhabitedType is always redundant
            if isinstance(proper_ti, UninhabitedType):
                continue

            duplicate_index = -1
            # Quickly check if we've seen this type
            if proper_ti in seen:
                duplicate_index = seen[proper_ti]
            elif (
                isinstance(proper_ti, LiteralType)
                and unduplicated_literal_fallbacks is not None
                and proper_ti.fallback in unduplicated_literal_fallbacks
            ):
                # This is an optimisation for unions with many LiteralType
                # We've already checked for exact duplicates. This means that any super type of
                # the LiteralType must be a super type of its fallback. If we've gone through
                # the expensive loop below and found no super type for a previous LiteralType
                # with the same fallback, we can skip doing that work again and just add the type
                # to new_items
                pass
            else:
                # If not, check if we've seen a supertype of this type
                for j, tj in enumerate(new_items):
                    tj = get_proper_type(tj)
                    # If tj is an Instance with a last_known_value, do not remove proper_ti
                    # (unless it's an instance with the same last_known_value)
                    if (
                        isinstance(tj, Instance)
                        and tj.last_known_value is not None
                        and not (
                            isinstance(proper_ti, Instance)
                            and tj.last_known_value == proper_ti.last_known_value
                        )
                    ):
                        continue

                    if is_proper_subtype(
                        ti, tj, keep_erased_types=keep_erased, ignore_promotions=True
                    ):
                        duplicate_index = j
                        break
            if duplicate_index != -1:
                # If deleted subtypes had more general truthiness, use that
                orig_item = new_items[duplicate_index]
                if not orig_item.can_be_true and ti.can_be_true:
                    new_items[duplicate_index] = true_or_false(orig_item)
                elif not orig_item.can_be_false and ti.can_be_false:
                    new_items[duplicate_index] = true_or_false(orig_item)
            else:
                # We have a non-duplicate item, add it to new_items
                seen[proper_ti] = len(new_items)
                new_items.append(ti)
                if isinstance(proper_ti, LiteralType):
                    if unduplicated_literal_fallbacks is None:
                        unduplicated_literal_fallbacks = set()
                    unduplicated_literal_fallbacks.add(proper_ti.fallback)

        items = new_items
        if len(items) <= 1:
            break
        items.reverse()

    return items


def make_simplified_intersection(
    items: Sequence[Type], line: int = -1, column: int = -1, *, keep_erased: bool = False
) -> ProperType:
    """Build intersection type with redundant intersection items removed.

    If only a single item remains, this may return a non-intersection type.

    Examples:

    * [int, str] -> Intersection[int, str]
    * [int, object] -> int
    * [int, int] -> int
    * [int, Any] -> Intersection[int, Any] (Any types are not simplified away!)
    * [Any, Any] -> Any
    * [int, Intersection[bytes, str]] -> Intersection[int, bytes, str]

    Note: This must NOT be used during semantic analysis, since TypeInfos may not
          be fully initialized.

    The keep_erased flag is used for type inference against intersection types
    containing type variables. If set to True, keep all ErasedType items.
    """
    # Step 1: expand all nested unions
    items = flatten_nested_unions(items, type_type=IntersectionType)

    # Step 2: fast path for single item
    if len(items) == 1:
        return get_proper_type(items[0])

    # Step 3: remove redundant intersections
    simplified_set: Sequence[Type] = _remove_redundant_intersection_items(items, keep_erased)

    result = get_proper_type(IntersectionType.make_intersection(simplified_set, line, column))

    # Step 5: At last, we erase any (inconsistent) extra attributes on instances.

    # Initialize with None instead of an empty set as a micro-optimization. The set
    # is needed very rarely, so we try to avoid constructing it.
    extra_attrs_set: set[ExtraAttrs] | None = None
    for item in items:
        instance = try_getting_instance_fallback(item)
        if instance and instance.extra_attrs:
            if extra_attrs_set is None:
                extra_attrs_set = {instance.extra_attrs}
            else:
                extra_attrs_set.add(instance.extra_attrs)

    if extra_attrs_set is not None and len(extra_attrs_set) > 1:
        fallback = try_getting_instance_fallback(result)
        if fallback:
            fallback.extra_attrs = None

    return result


def _remove_redundant_intersection_items(items: list[Type], keep_erased: bool) -> list[Type]:
    from mypy.subtypes import is_proper_subtype

    removed: set[int] = set()

    for outer_i in range(len(items)):
        proper_outer = get_proper_type(items[outer_i])
        for inner_i in range(outer_i + 1, len(items)):
            if inner_i in removed:
                continue
            proper_inner = get_proper_type(items[inner_i])
            if is_proper_subtype(
                proper_outer, proper_inner, keep_erased_types=keep_erased, ignore_promotions=True
            ):
                removed.add(inner_i)
            elif is_proper_subtype(
                proper_inner, proper_outer, keep_erased_types=keep_erased, ignore_promotions=True
            ):
                removed.add(outer_i)

    return [items[i] for i in range(len(items)) if i not in removed]


def _get_type_special_method_bool_ret_type(t: Type) -> Type | None:
    t = get_proper_type(t)

    if isinstance(t, Instance):
        bool_method = t.type.get("__bool__")
        if bool_method:
            callee = get_proper_type(bool_method.type)
            if isinstance(callee, CallableType):
                return callee.ret_type

    return None


def true_only(t: Type) -> ProperType:
    """
    Restricted version of t with only True-ish values
    """
    t = get_proper_type(t)

    if not t.can_be_true:
        # All values of t are False-ish, so there are no true values in it
        return UninhabitedType(line=t.line, column=t.column)
    elif not t.can_be_false:
        # All values of t are already True-ish, so true_only is idempotent in this case
        return t
    elif isinstance(t, UnionType):
        # The true version of a union type is the union of the true versions of its components
        new_items = [true_only(item) for item in t.items]
        can_be_true_items = [item for item in new_items if item.can_be_true]
        return make_simplified_union(can_be_true_items, line=t.line, column=t.column)
    else:
        ret_type = _get_type_special_method_bool_ret_type(t)

        if ret_type and ret_type.can_be_false and not ret_type.can_be_true:
            new_t = copy_type(t)
            new_t.can_be_true = False
            return new_t

        new_t = copy_type(t)
        new_t.can_be_false = False
        return new_t


def false_only(t: Type) -> ProperType:
    """
    Restricted version of t with only False-ish values
    """
    t = get_proper_type(t)

    if not t.can_be_false:
        if state.strict_optional:
            # All values of t are True-ish, so there are no false values in it
            return UninhabitedType(line=t.line)
        else:
            # When strict optional checking is disabled, everything can be
            # False-ish since anything can be None
            return NoneType(line=t.line)
    elif not t.can_be_true:
        # All values of t are already False-ish, so false_only is idempotent in this case
        return t
    elif isinstance(t, UnionType):
        # The false version of a union type is the union of the false versions of its components
        new_items = [false_only(item) for item in t.items]
        can_be_false_items = [item for item in new_items if item.can_be_false]
        return make_simplified_union(can_be_false_items, line=t.line, column=t.column)
    else:
        ret_type = _get_type_special_method_bool_ret_type(t)

        if ret_type and ret_type.can_be_true and not ret_type.can_be_false:
            new_t = copy_type(t)
            new_t.can_be_false = False
            return new_t

        new_t = copy_type(t)
        new_t.can_be_true = False
        return new_t


def true_or_false(t: Type) -> ProperType:
    """
    Unrestricted version of t with both True-ish and False-ish values
    """
    t = get_proper_type(t)

    if isinstance(t, UnionType):
        new_items = [true_or_false(item) for item in t.items]
        return make_simplified_union(new_items, line=t.line, column=t.column)

    new_t = copy_type(t)
    new_t.can_be_true = new_t.can_be_true_default()
    new_t.can_be_false = new_t.can_be_false_default()
    return new_t


def erase_def_to_union_or_bound(tdef: TypeVarLikeType) -> Type:
    # TODO(PEP612): fix for ParamSpecType
    if isinstance(tdef, ParamSpecType):
        return AnyType(TypeOfAny.from_error)
    assert isinstance(tdef, TypeVarType)
    if tdef.values:
        return make_simplified_union(tdef.values)
    else:
        return tdef.upper_bound


def erase_to_union_or_bound(typ: TypeVarType) -> ProperType:
    if typ.values:
        return make_simplified_union(typ.values)
    else:
        return get_proper_type(typ.upper_bound)


def function_type(func: FuncBase, fallback: Instance) -> FunctionLike:
    if func.type:
        assert isinstance(func.type, FunctionLike)
        return func.type
    else:
        # Implicit type signature with dynamic types.
        if isinstance(func, FuncItem):
            return callable_type(func, fallback)
        else:
            # Broken overloads can have self.type set to None.
            # TODO: should we instead always set the type in semantic analyzer?
            assert isinstance(func, OverloadedFuncDef)
            any_type = AnyType(TypeOfAny.from_error)
            dummy = CallableType(
                [any_type, any_type],
                [ARG_STAR, ARG_STAR2],
                [None, None],
                any_type,
                fallback,
                line=func.line,
                is_ellipsis_args=True,
            )
            # Return an Overloaded, because some callers may expect that
            # an OverloadedFuncDef has an Overloaded type.
            return Overloaded([dummy])


def callable_type(
    fdef: FuncItem, fallback: Instance, ret_type: Type | None = None
) -> CallableType:
    # TODO: somewhat unfortunate duplication with prepare_method_signature in semanal
    if fdef.info and (not fdef.is_static or fdef.name == "__new__") and fdef.arg_names:
        self_type: Type = fill_typevars(fdef.info)
        if fdef.is_class or fdef.name == "__new__":
            self_type = TypeType.make_normalized(self_type)
        args = [self_type] + [UntypedType()] * (len(fdef.arg_names) - 1)
    else:
        args = [UntypedType()] * len(fdef.arg_names)

    return CallableType(
        args,
        fdef.arg_kinds,
        fdef.arg_names,
        ret_type or UntypedType(),
        fallback,
        name=fdef.name,
        line=fdef.line,
        column=fdef.column,
        implicit=True,
        # We need this for better error messages, like missing `self` note:
        definition=fdef if isinstance(fdef, FuncDef) else None,
    )


def try_getting_str_literals(expr: Expression, typ: Type) -> list[str] | None:
    """If the given expression or type corresponds to a string literal
    or a union of string literals, returns a list of the underlying strings.
    Otherwise, returns None.

    Specifically, this function is guaranteed to return a list with
    one or more strings if one of the following is true:

    1. 'expr' is a StrExpr
    2. 'typ' is a LiteralType containing a string
    3. 'typ' is a UnionType containing only LiteralType of strings
    """
    if isinstance(expr, StrExpr):
        return [expr.value]

    # TODO: See if we can eliminate this function and call the below one directly
    return try_getting_str_literals_from_type(typ)


def try_getting_str_literals_from_type(typ: Type) -> list[str] | None:
    """If the given expression or type corresponds to a string Literal
    or a union of string Literals, returns a list of the underlying strings.
    Otherwise, returns None.

    For example, if we had the type 'Literal["foo", "bar"]' as input, this function
    would return a list of strings ["foo", "bar"].
    """
    return try_getting_literals_from_type(typ, str, "builtins.str")


def try_getting_int_literals_from_type(typ: Type) -> list[int] | None:
    """If the given expression or type corresponds to an int Literal
    or a union of int Literals, returns a list of the underlying ints.
    Otherwise, returns None.

    For example, if we had the type 'Literal[1, 2, 3]' as input, this function
    would return a list of ints [1, 2, 3].
    """
    return try_getting_literals_from_type(typ, int, "builtins.int")


T = TypeVar("T")


def try_getting_literals_from_type(
    typ: Type, target_literal_type: type[T], target_fullname: str
) -> list[T] | None:
    """If the given expression or type corresponds to a Literal or
    union of Literals where the underlying values correspond to the given
    target type, returns a list of those underlying values. Otherwise,
    returns None.
    """
    typ = get_proper_type(typ)

    if isinstance(typ, Instance) and typ.last_known_value is not None:
        possible_literals: list[Type] = [typ.last_known_value]
    elif isinstance(typ, UnionType):
        possible_literals = list(typ.items)
    else:
        possible_literals = [typ]

    literals: list[T] = []
    for lit in get_proper_types(possible_literals):
        if isinstance(lit, LiteralType) and lit.fallback.type.fullname == target_fullname:
            val = lit.value
            if isinstance(val, target_literal_type):
                literals.append(val)
            else:
                return None
        else:
            return None
    return literals


def is_literal_type_like(t: Type | None) -> bool:
    """Returns 'true' if the given type context is potentially either a LiteralType,
    a Union of LiteralType, or something similar.
    """
    t = get_proper_type(t)
    if t is None:
        return False
    elif isinstance(t, LiteralType):
        return True
    elif isinstance(t, UnionType):
        return any(is_literal_type_like(item) for item in t.items)
    elif isinstance(t, TypeVarType):
        return is_literal_type_like(t.upper_bound) or any(
            is_literal_type_like(item) for item in t.values
        )
    else:
        return False


def is_singleton_type(typ: Type) -> bool:
    """Returns 'true' if this type is a "singleton type" -- if there exists
    exactly only one runtime value associated with this type.

    That is, given two values 'a' and 'b' that have the same type 't',
    'is_singleton_type(t)' returns True if and only if the expression 'a is b' is
    always true.

    Currently, this returns True when given NoneTypes, enum LiteralTypes,
    enum types with a single value and ... (Ellipses).

    Note that other kinds of LiteralTypes cannot count as singleton types. For
    example, suppose we do 'a = 100000 + 1' and 'b = 100001'. It is not guaranteed
    that 'a is b' will always be true -- some implementations of Python will end up
    constructing two distinct instances of 100001.
    """
    typ = get_proper_type(typ)
    return typ.is_singleton_type()


def try_expanding_sum_type_to_union(typ: Type, target_fullname: str) -> ProperType:
    """Attempts to recursively expand any enum Instances with the given target_fullname
    into a Union of all of its component LiteralTypes.

    For example, if we have:

        class Color(Enum):
            RED = 1
            BLUE = 2
            YELLOW = 3

        class Status(Enum):
            SUCCESS = 1
            FAILURE = 2
            UNKNOWN = 3

    ...and if we call `try_expanding_enum_to_union(Union[Color, Status], 'module.Color')`,
    this function will return Literal[Color.RED, Color.BLUE, Color.YELLOW, Status].
    """
    typ = get_proper_type(typ)

    if isinstance(typ, UnionType):
        items = [
            try_expanding_sum_type_to_union(item, target_fullname) for item in typ.relevant_items()
        ]
        return make_simplified_union(items, contract_literals=False)
    elif isinstance(typ, Instance) and typ.type.fullname == target_fullname:
        if typ.type.is_enum:
            new_items = []
            for name, symbol in typ.type.names.items():
                if not isinstance(symbol.node, Var):
                    continue
                # Skip these since Enum will remove it
                if name in ENUM_REMOVED_PROPS:
                    continue
                new_items.append(LiteralType(name, typ))
            return make_simplified_union(new_items, contract_literals=False)
        elif typ.type.fullname == "builtins.bool":
            return make_simplified_union(
                [LiteralType(True, typ), LiteralType(False, typ)], contract_literals=False
            )

    return typ


def try_contracting_literals_in_union(types: Sequence[Type]) -> list[ProperType]:
    """Contracts any literal types back into a sum type if possible.

    Will replace the first instance of the literal with the sum type and
    remove all others.

    If we call `try_contracting_union(Literal[Color.RED, Color.BLUE, Color.YELLOW])`,
    this function will return Color.

    We also treat `Literal[True, False]` as `bool`.
    """
    proper_types = [get_proper_type(typ) for typ in types]
    sum_types: dict[str, tuple[set[Any], list[int]]] = {}
    marked_for_deletion = set()
    for idx, typ in enumerate(proper_types):
        if isinstance(typ, LiteralType):
            fullname = typ.fallback.type.fullname
            if typ.fallback.type.is_enum or isinstance(typ.value, bool):
                if fullname not in sum_types:
                    sum_types[fullname] = (
                        set(typ.fallback.get_enum_values())
                        if typ.fallback.type.is_enum
                        else {True, False},
                        [],
                    )
                literals, indexes = sum_types[fullname]
                literals.discard(typ.value)
                indexes.append(idx)
                if not literals:
                    first, *rest = indexes
                    proper_types[first] = typ.fallback
                    marked_for_deletion |= set(rest)
    return list(
        itertools.compress(
            proper_types, [(i not in marked_for_deletion) for i in range(len(proper_types))]
        )
    )


def coerce_to_literal(typ: Type) -> Type:
    """Recursively converts any Instances that have a last_known_value or are
    instances of enum types with a single value into the corresponding LiteralType.
    """
    original_type = typ
    typ = get_proper_type(typ)
    if isinstance(typ, UnionType):
        new_items = [coerce_to_literal(item) for item in typ.items]
        return UnionType.make_union(new_items)
    elif isinstance(typ, Instance):
        if typ.last_known_value:
            return typ.last_known_value
        elif typ.type.is_enum:
            enum_values = typ.get_enum_values()
            if len(enum_values) == 1:
                return LiteralType(value=enum_values[0], fallback=typ)
    return original_type


def get_type_vars(tp: Type) -> list[TypeVarType]:
    return tp.accept(TypeVarExtractor())


class TypeVarExtractor(TypeQuery[List[TypeVarType]]):
    def __init__(self) -> None:
        super().__init__(self._merge)

    def _merge(self, iter: Iterable[list[TypeVarType]]) -> list[TypeVarType]:
        out = []
        for item in iter:
            out.extend(item)
        return out

    def visit_type_var(self, t: TypeVarType) -> list[TypeVarType]:
        return [t]


def custom_special_method(typ: Type, name: str, check_all: bool = False) -> bool:
    """Does this type have a custom special method such as __format__() or __eq__()?

    If check_all is True ensure all items of a union have a custom method, not just some.
    """
    typ = get_proper_type(typ)
    if isinstance(typ, Instance):
        method = typ.type.get(name)
        if method and isinstance(method.node, (SYMBOL_FUNCBASE_TYPES, Decorator, Var)):
            if method.node.info:
                return not method.node.info.fullname.startswith("builtins.")
        return False
    if isinstance(typ, UnionType):
        if check_all:
            return all(custom_special_method(t, name, check_all) for t in typ.items)
        return any(custom_special_method(t, name) for t in typ.items)
    if isinstance(typ, TupleType):
        return custom_special_method(tuple_fallback(typ), name, check_all)
    if isinstance(typ, CallableType) and typ.is_type_obj():
        # Look up __method__ on the metaclass for class objects.
        return custom_special_method(typ.fallback, name, check_all)
    if isinstance(typ, AnyType):
        # Avoid false positives in uncertain cases.
        return True
    # TODO: support other types (see ExpressionChecker.has_member())?
    return False


def separate_union_literals(t: UnionType) -> tuple[Sequence[LiteralType], Sequence[Type]]:
    """Separate literals from other members in a union type."""
    literal_items = []
    union_items = []

    for item in t.items:
        proper = get_proper_type(item)
        if isinstance(proper, LiteralType):
            literal_items.append(proper)
        else:
            union_items.append(item)

    return literal_items, union_items


def infer_impl_from_parts(
    impl: OverloadPart,
    types: list[CallableType],
    fallback: Instance,
    named_type: Callable[[str, list[Type]], Type],
):
    impl_func = impl if isinstance(impl, FuncDef) else impl.func
    # infer the types of the impl from the overload types
    arg_types: dict[str | int, list[Type]] = defaultdict(list)
    ret_types = []
    for tp in types:
        for i, arg_type in enumerate(tp.arg_types):
            arg_name = tp.arg_names[i]
            if not arg_name:  # if it's positional only
                if arg_type not in arg_types[i]:
                    arg_types[i].append(arg_type)
            else:
                if arg_name in impl_func.arg_names:
                    if arg_type not in arg_types[arg_name]:
                        arg_types[arg_name].append(arg_type)
                if arg_name and arg_name in impl_func.arg_names:
                    if arg_type not in arg_types[arg_name]:
                        arg_types[arg_name].append(arg_type)
        t = get_proper_type(tp.ret_type)
        if isinstance(t, Instance) and t.type.fullname == "typing.Coroutine":
            ret_type = t.args[2]
        else:
            ret_type = tp.ret_type
        if ret_type not in ret_types:
            ret_types.append(ret_type)
    res_arg_types = [
        UnionType.make_union((arg_types[arg_name_] if arg_name_ else []) + arg_types[i])
        if arg_kind not in (ARG_STAR, ARG_STAR2)
        else UntypedType()
        for i, (arg_name_, arg_kind) in enumerate(zip(impl_func.arg_names, impl_func.arg_kinds))
    ]

    ret_type = UnionType.make_union(ret_types)

    if impl_func.is_coroutine:
        # if the impl is a coroutine, then assume the parts are also, if not need annotation
        any_type = AnyType(TypeOfAny.special_form)
        ret_type = named_type("typing.Coroutine", [any_type, any_type, ret_type])

    # use unanalyzed_type because we would have already tried to infer from defaults
    if impl_func.unanalyzed_type:
        assert isinstance(impl_func.unanalyzed_type, CallableType)
        assert isinstance(impl_func.type, CallableType)
        impl_func.type = impl_func.type.copy_modified(
            arg_types=[
                i if not is_unannotated_any(u) else r
                for i, u, r in zip(
                    impl_func.type.arg_types, impl_func.unanalyzed_type.arg_types, res_arg_types
                )
            ],
            ret_type=ret_type
            if isinstance(get_proper_type(impl_func.unanalyzed_type.ret_type), (AnyType, NoneType))
            else impl_func.type.ret_type,
        )
    else:
        impl_func.type = CallableType(
            res_arg_types,
            impl_func.arg_kinds,
            impl_func.arg_names,
            ret_type,
            fallback,
            definition=impl_func,
        )


def try_getting_instance_fallback(typ: Type) -> Instance | None:
    """Returns the Instance fallback for this type if one exists or None."""
    typ = get_proper_type(typ)
    if isinstance(typ, Instance):
        return typ
    elif isinstance(typ, LiteralType):
        return typ.fallback
    elif isinstance(typ, NoneType):
        return None  # Fast path for None, which is common
    elif isinstance(typ, FunctionLike):
        return typ.fallback
    elif isinstance(typ, TupleType):
        return typ.partial_fallback
    elif isinstance(typ, TypedDictType):
        return typ.fallback
    elif isinstance(typ, TypeVarType):
        return try_getting_instance_fallback(typ.upper_bound)
    return None


def fixup_partial_type(typ: Type) -> Type:
    """Convert a partial type that we couldn't resolve into something concrete.

    This means, for None we make it Optional[Any], and for anything else we
    fill in all of the type arguments with Any.
    """
    if not isinstance(typ, PartialType):
        return typ
    if typ.type is None:
        return UnionType.make_union([AnyType(TypeOfAny.unannotated), NoneType()])
    else:
        return Instance(typ.type, [AnyType(TypeOfAny.unannotated)] * len(typ.type.type_vars))


def get_protocol_member(left: Instance, member: str, class_obj: bool) -> ProperType | None:
    if member == "__call__" and class_obj:
        # Special case: class objects always have __call__ that is just the constructor.
        from mypy.checkmember import type_object_type

        def named_type(fullname: str) -> Instance:
            return Instance(left.type.mro[-1], [])

        return type_object_type(left.type, named_type)

    if member == "__call__" and left.type.is_metaclass():
        # Special case: we want to avoid falling back to metaclass __call__
        # if constructor signature didn't match, this can cause many false negatives.
        return None

    from mypy.subtypes import find_member

    return get_proper_type(find_member(member, left, left, class_obj=class_obj))
