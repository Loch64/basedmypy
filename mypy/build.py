"""Facilities to analyze entire programs, including imported modules.

Parse and analyze the source files of a program in the correct order
(based on file dependencies), and collect the results.

This module only directs a build, which is performed in multiple passes per
file.  The individual passes are implemented in separate modules.

The function build() is the main interface to this module.
"""
# TODO: More consistent terminology, e.g. path/fnam, module/id, state/file

import contextlib
import errno
import gc
import json
import os
import platform
import re
import stat
import sys
import time
import types

from typing import (AbstractSet, Any, Dict, Iterable, Iterator, List, Sequence,
                    Mapping, NamedTuple, Optional, Set, Tuple, TypeVar, Union, Callable, TextIO)
from typing_extensions import ClassVar, Final, TYPE_CHECKING, TypeAlias as _TypeAlias
from mypy_extensions import TypedDict

from mypy.nodes import MypyFile, ImportBase, Import, ImportFrom, ImportAll, SymbolTable
from mypy.semanal_pass1 import SemanticAnalyzerPreAnalysis
from mypy.semanal import SemanticAnalyzer
import mypy.semanal_main
from mypy.checker import TypeChecker
from mypy.indirection import TypeIndirectionVisitor
from mypy.errors import Errors, CompileError, ErrorInfo, report_internal_error
from mypy.util import (
    DecodeError, decode_python_encoding, is_sub_path, get_mypy_comments, module_prefix,
    read_py_file, hash_digest, is_typeshed_file, is_stub_package_file, get_top_two_prefixes
)
if TYPE_CHECKING:
    from mypy.report import Reports  # Avoid unconditional slow import
from mypy.fixup import fixup_module
from mypy.modulefinder import (
    BuildSource, compute_search_paths, FindModuleCache, SearchPaths, ModuleSearchResult,
    ModuleNotFoundReason
)
from mypy.nodes import Expression
from mypy.options import Options
from mypy.parse import parse
from mypy.stats import dump_type_stats
from mypy.types import Type
from mypy.version import __version__
from mypy.plugin import Plugin, ChainedPlugin, ReportConfigContext
from mypy.plugins.default import DefaultPlugin
from mypy.fscache import FileSystemCache
from mypy.metastore import MetadataStore, FilesystemMetadataStore, SqliteMetadataStore
from mypy.typestate import TypeState, reset_global_state
from mypy.renaming import VariableRenameVisitor
from mypy.config_parser import parse_mypy_comments
from mypy.freetree import free_tree
from mypy.stubinfo import legacy_bundled_packages, is_legacy_bundled_package
from mypy import errorcodes as codes


# Switch to True to produce debug output related to fine-grained incremental
# mode only that is useful during development. This produces only a subset of
# output compared to --verbose output. We use a global flag to enable this so
# that it's easy to enable this when running tests.
DEBUG_FINE_GRAINED: Final = False

# These modules are special and should always come from typeshed.
CORE_BUILTIN_MODULES: Final = {
    'builtins',
    'typing',
    'types',
    'typing_extensions',
    'mypy_extensions',
    '_importlib_modulespec',
    'sys',
    'abc',
}


Graph: _TypeAlias = Dict[str, 'State']


# TODO: Get rid of BuildResult.  We might as well return a BuildManager.
class BuildResult:
    """The result of a successful build.

    Attributes:
      manager: The build manager.
      files:   Dictionary from module name to related AST node.
      types:   Dictionary from parse tree node to its inferred type.
      used_cache: Whether the build took advantage of a pre-existing cache
      errors:  List of error messages.
    """

    def __init__(self, manager: 'BuildManager', graph: Graph) -> None:
        self.manager = manager
        self.graph = graph
        self.files = manager.modules
        self.types = manager.all_types  # Non-empty if export_types True in options
        self.used_cache = manager.cache_enabled
        self.errors: List[str] = []  # Filled in by build if desired


class BuildSourceSet:
    """Efficiently test a file's membership in the set of build sources."""

    def __init__(self, sources: List[BuildSource]) -> None:
        self.source_text_present = False
        self.source_modules: Set[str] = set()
        self.source_paths: Set[str] = set()

        for source in sources:
            if source.text is not None:
                self.source_text_present = True
            elif source.path:
                self.source_paths.add(source.path)
            else:
                self.source_modules.add(source.module)

    def is_source(self, file: MypyFile) -> bool:
        if file.path and file.path in self.source_paths:
            return True
        elif file._fullname in self.source_modules:
            return True
        elif self.source_text_present:
            return True
        else:
            return False


def build(sources: List[BuildSource],
          options: Options,
          alt_lib_path: Optional[str] = None,
          flush_errors: Optional[Callable[[List[str], bool], None]] = None,
          fscache: Optional[FileSystemCache] = None,
          stdout: Optional[TextIO] = None,
          stderr: Optional[TextIO] = None,
          extra_plugins: Optional[Sequence[Plugin]] = None,
          ) -> BuildResult:
    """Analyze a program.

    A single call to build performs parsing, semantic analysis and optionally
    type checking for the program *and* all imported modules, recursively.

    Return BuildResult if successful or only non-blocking errors were found;
    otherwise raise CompileError.

    If a flush_errors callback is provided, all error messages will be
    passed to it and the errors and messages fields of BuildResult and
    CompileError (respectively) will be empty. Otherwise those fields will
    report any error messages.

    Args:
      sources: list of sources to build
      options: build options
      alt_lib_path: an additional directory for looking up library modules
        (takes precedence over other directories)
      flush_errors: optional function to flush errors after a file is processed
      fscache: optionally a file-system cacher

    """
    # If we were not given a flush_errors, we use one that will populate those
    # fields for callers that want the traditional API.
    messages = []

    def default_flush_errors(new_messages: List[str], is_serious: bool) -> None:
        messages.extend(new_messages)

    flush_errors = flush_errors or default_flush_errors
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    extra_plugins = extra_plugins or []

    try:
        result = _build(
            sources, options, alt_lib_path, flush_errors, fscache, stdout, stderr, extra_plugins
        )
        result.errors = messages
        return result
    except CompileError as e:
        # CompileErrors raised from an errors object carry all of the
        # messages that have not been reported out by error streaming.
        # Patch it up to contain either none or all none of the messages,
        # depending on whether we are flushing errors.
        serious = not e.use_stdout
        flush_errors(e.messages, serious)
        e.messages = messages
        raise


def _build(sources: List[BuildSource],
           options: Options,
           alt_lib_path: Optional[str],
           flush_errors: Callable[[List[str], bool], None],
           fscache: Optional[FileSystemCache],
           stdout: TextIO,
           stderr: TextIO,
           extra_plugins: Sequence[Plugin],
           ) -> BuildResult:
    if platform.python_implementation() == 'CPython':
        # This seems the most reasonable place to tune garbage collection.
        gc.set_threshold(150 * 1000)

    data_dir = default_data_dir()
    fscache = fscache or FileSystemCache()

    search_paths = compute_search_paths(sources, options, data_dir, alt_lib_path)

    reports = None
    if options.report_dirs:
        # Import lazily to avoid slowing down startup.
        from mypy.report import Reports  # noqa
        reports = Reports(data_dir, options.report_dirs)

    source_set = BuildSourceSet(sources)
    cached_read = fscache.read
    errors = Errors(options.show_error_context,
                    options.show_column_numbers,
                    options.show_error_codes,
                    options.pretty,
                    lambda path: read_py_file(path, cached_read, options.python_version),
                    options.show_absolute_path,
                    options.enabled_error_codes,
                    options.disabled_error_codes,
                    options.many_errors_threshold)
    plugin, snapshot = load_plugins(options, errors, stdout, extra_plugins)

    # Add catch-all .gitignore to cache dir if we created it
    cache_dir_existed = os.path.isdir(options.cache_dir)

    # Construct a build manager object to hold state during the build.
    #
    # Ignore current directory prefix in error messages.
    manager = BuildManager(data_dir, search_paths,
                           ignore_prefix=os.getcwd(),
                           source_set=source_set,
                           reports=reports,
                           options=options,
                           version_id=__version__,
                           plugin=plugin,
                           plugins_snapshot=snapshot,
                           errors=errors,
                           flush_errors=flush_errors,
                           fscache=fscache,
                           stdout=stdout,
                           stderr=stderr)
    manager.trace(repr(options))

    reset_global_state()
    try:
        graph = dispatch(sources, manager, stdout)
        if not options.fine_grained_incremental:
            TypeState.reset_all_subtype_caches()
        return BuildResult(manager, graph)
    finally:
        t0 = time.time()
        manager.metastore.commit()
        manager.add_stats(cache_commit_time=time.time() - t0)
        manager.log("Build finished in %.3f seconds with %d modules, and %d errors" %
                    (time.time() - manager.start_time,
                     len(manager.modules),
                     manager.errors.num_messages()))
        manager.dump_stats()
        if reports is not None:
            # Finish the HTML or XML reports even if CompileError was raised.
            reports.finish()
        if not cache_dir_existed and os.path.isdir(options.cache_dir):
            add_catch_all_gitignore(options.cache_dir)
            exclude_from_backups(options.cache_dir)
        if os.path.isdir(options.cache_dir):
            record_missing_stub_packages(options.cache_dir, manager.missing_stub_packages)


def default_data_dir() -> str:
    """Returns directory containing typeshed directory."""
    return os.path.dirname(__file__)


def normpath(path: str, options: Options) -> str:
    """Convert path to absolute; but to relative in bazel mode.

    (Bazel's distributed cache doesn't like filesystem metadata to
    end up in output files.)
    """
    # TODO: Could we always use relpath?  (A worry in non-bazel
    # mode would be that a moved file may change its full module
    # name without changing its size, mtime or hash.)
    if options.bazel:
        return os.path.relpath(path)
    else:
        return os.path.abspath(path)


CacheMeta = NamedTuple('CacheMeta',
                       [('id', str),
                        ('path', str),
                        ('mtime', int),
                        ('size', int),
                        ('hash', str),
                        ('dependencies', List[str]),  # names of imported modules
                        ('data_mtime', int),  # mtime of data_json
                        ('data_json', str),  # path of <id>.data.json
                        ('suppressed', List[str]),  # dependencies that weren't imported
                        ('options', Optional[Dict[str, object]]),  # build options
                        # dep_prios and dep_lines are in parallel with
                        # dependencies + suppressed.
                        ('dep_prios', List[int]),
                        ('dep_lines', List[int]),
                        ('interface_hash', str),  # hash representing the public interface
                        ('version_id', str),  # mypy version for cache invalidation
                        ('ignore_all', bool),  # if errors were ignored
                        ('plugin_data', Any),  # config data from plugins
                        ])
# NOTE: dependencies + suppressed == all reachable imports;
# suppressed contains those reachable imports that were prevented by
# silent mode or simply not found.

# Metadata for the fine-grained dependencies file associated with a module.
FgDepMeta = TypedDict('FgDepMeta', {'path': str, 'mtime': int})


def cache_meta_from_dict(meta: Dict[str, Any], data_json: str) -> CacheMeta:
    """Build a CacheMeta object from a json metadata dictionary

    Args:
      meta: JSON metadata read from the metadata cache file
      data_json: Path to the .data.json file containing the AST trees
    """
    sentinel: Any = None  # Values to be validated by the caller
    return CacheMeta(
        meta.get('id', sentinel),
        meta.get('path', sentinel),
        int(meta['mtime']) if 'mtime' in meta else sentinel,
        meta.get('size', sentinel),
        meta.get('hash', sentinel),
        meta.get('dependencies', []),
        int(meta['data_mtime']) if 'data_mtime' in meta else sentinel,
        data_json,
        meta.get('suppressed', []),
        meta.get('options'),
        meta.get('dep_prios', []),
        meta.get('dep_lines', []),
        meta.get('interface_hash', ''),
        meta.get('version_id', sentinel),
        meta.get('ignore_all', True),
        meta.get('plugin_data', None),
    )


# Priorities used for imports.  (Here, top-level includes inside a class.)
# These are used to determine a more predictable order in which the
# nodes in an import cycle are processed.
PRI_HIGH: Final = 5  # top-level "from X import blah"
PRI_MED: Final = 10  # top-level "import X"
PRI_LOW: Final = 20  # either form inside a function
PRI_MYPY: Final = 25  # inside "if MYPY" or "if TYPE_CHECKING"
PRI_INDIRECT: Final = 30  # an indirect dependency
PRI_ALL: Final = 99  # include all priorities


def import_priority(imp: ImportBase, toplevel_priority: int) -> int:
    """Compute import priority from an import node."""
    if not imp.is_top_level:
        # Inside a function
        return PRI_LOW
    if imp.is_mypy_only:
        # Inside "if MYPY" or "if typing.TYPE_CHECKING"
        return max(PRI_MYPY, toplevel_priority)
    # A regular import; priority determined by argument.
    return toplevel_priority


def load_plugins_from_config(
    options: Options, errors: Errors, stdout: TextIO
) -> Tuple[List[Plugin], Dict[str, str]]:
    """Load all configured plugins.

    Return a list of all the loaded plugins from the config file.
    The second return value is a snapshot of versions/hashes of loaded user
    plugins (for cache validation).
    """
    import importlib

    snapshot: Dict[str, str] = {}

    if not options.config_file:
        return [], snapshot

    line = find_config_file_line_number(options.config_file, 'mypy', 'plugins')
    if line == -1:
        line = 1  # We need to pick some line number that doesn't look too confusing

    def plugin_error(message: str) -> None:
        errors.report(line, 0, message)
        errors.raise_error(use_stdout=False)

    custom_plugins: List[Plugin] = []
    errors.set_file(options.config_file, None)
    for plugin_path in options.plugins:
        func_name = 'plugin'
        plugin_dir: Optional[str] = None
        if ':' in os.path.basename(plugin_path):
            plugin_path, func_name = plugin_path.rsplit(':', 1)
        if plugin_path.endswith('.py'):
            # Plugin paths can be relative to the config file location.
            plugin_path = os.path.join(os.path.dirname(options.config_file), plugin_path)
            if not os.path.isfile(plugin_path):
                plugin_error('Can\'t find plugin "{}"'.format(plugin_path))
            # Use an absolute path to avoid populating the cache entry
            # for 'tmp' during tests, since it will be different in
            # different tests.
            plugin_dir = os.path.abspath(os.path.dirname(plugin_path))
            fnam = os.path.basename(plugin_path)
            module_name = fnam[:-3]
            sys.path.insert(0, plugin_dir)
        elif re.search(r'[\\/]', plugin_path):
            fnam = os.path.basename(plugin_path)
            plugin_error('Plugin "{}" does not have a .py extension'.format(fnam))
        else:
            module_name = plugin_path

        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            plugin_error('Error importing plugin "{}": {}'.format(plugin_path, exc))
        finally:
            if plugin_dir is not None:
                assert sys.path[0] == plugin_dir
                del sys.path[0]

        if not hasattr(module, func_name):
            plugin_error('Plugin "{}" does not define entry point function "{}"'.format(
                plugin_path, func_name))

        try:
            plugin_type = getattr(module, func_name)(__version__)
        except Exception:
            print('Error calling the plugin(version) entry point of {}\n'.format(plugin_path),
                  file=stdout)
            raise  # Propagate to display traceback

        if not isinstance(plugin_type, type):
            plugin_error(
                'Type object expected as the return value of "plugin"; got {!r} (in {})'.format(
                    plugin_type, plugin_path))
        if not issubclass(plugin_type, Plugin):
            plugin_error(
                'Return value of "plugin" must be a subclass of "mypy.plugin.Plugin" '
                '(in {})'.format(plugin_path))
        try:
            custom_plugins.append(plugin_type(options))
            snapshot[module_name] = take_module_snapshot(module)
        except Exception:
            print('Error constructing plugin instance of {}\n'.format(plugin_type.__name__),
                  file=stdout)
            raise  # Propagate to display traceback

    return custom_plugins, snapshot


def load_plugins(options: Options,
                 errors: Errors,
                 stdout: TextIO,
                 extra_plugins: Sequence[Plugin],
                 ) -> Tuple[Plugin, Dict[str, str]]:
    """Load all configured plugins.

    Return a plugin that encapsulates all plugins chained together. Always
    at least include the default plugin (it's last in the chain).
    The second return value is a snapshot of versions/hashes of loaded user
    plugins (for cache validation).
    """
    custom_plugins, snapshot = load_plugins_from_config(options, errors, stdout)

    custom_plugins += extra_plugins

    default_plugin: Plugin = DefaultPlugin(options)
    if not custom_plugins:
        return default_plugin, snapshot

    # Custom plugins take precedence over the default plugin.
    return ChainedPlugin(options, custom_plugins + [default_plugin]), snapshot


def take_module_snapshot(module: types.ModuleType) -> str:
    """Take plugin module snapshot by recording its version and hash.

    We record _both_ hash and the version to detect more possible changes
    (e.g. if there is a change in modules imported by a plugin).
    """
    if hasattr(module, '__file__'):
        assert module.__file__ is not None
        with open(module.__file__, 'rb') as f:
            digest = hash_digest(f.read())
    else:
        digest = 'unknown'
    ver = getattr(module, '__version__', 'none')
    return '{}:{}'.format(ver, digest)


def find_config_file_line_number(path: str, section: str, setting_name: str) -> int:
    """Return the approximate location of setting_name within mypy config file.

    Return -1 if can't determine the line unambiguously.
    """
    in_desired_section = False
    try:
        results = []
        with open(path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1].strip()
                    in_desired_section = (current_section == section)
                elif in_desired_section and re.match(r'{}\s*='.format(setting_name), line):
                    results.append(i + 1)
        if len(results) == 1:
            return results[0]
    except OSError:
        pass
    return -1


class BuildManager:
    """This class holds shared state for building a mypy program.

    It is used to coordinate parsing, import processing, semantic
    analysis and type checking.  The actual build steps are carried
    out by dispatch().

    Attributes:
      data_dir:        Mypy data directory (contains stubs)
      search_paths:    SearchPaths instance indicating where to look for modules
      modules:         Mapping of module ID to MypyFile (shared by the passes)
      semantic_analyzer:
                       Semantic analyzer, pass 2
      all_types:       Map {Expression: Type} from all modules (enabled by export_types)
      options:         Build options
      missing_modules: Set of modules that could not be imported encountered so far
      stale_modules:   Set of modules that needed to be rechecked (only used by tests)
      fg_deps_meta:    Metadata for fine-grained dependencies caches associated with modules
      fg_deps:         A fine-grained dependency map
      version_id:      The current mypy version (based on commit id when possible)
      plugin:          Active mypy plugin(s)
      plugins_snapshot:
                       Snapshot of currently active user plugins (versions and hashes)
      old_plugins_snapshot:
                       Plugins snapshot from previous incremental run (or None in
                       non-incremental mode and if cache was not found)
      errors:          Used for reporting all errors
      flush_errors:    A function for processing errors after each SCC
      cache_enabled:   Whether cache is being read. This is set based on options,
                       but is disabled if fine-grained cache loading fails
                       and after an initial fine-grained load. This doesn't
                       determine whether we write cache files or not.
      quickstart_state:
                       A cache of filename -> mtime/size/hash info used to avoid
                       needing to hash source files when using a cache with mismatching mtimes
      stats:           Dict with various instrumentation numbers, it is used
                       not only for debugging, but also required for correctness,
                       in particular to check consistency of the fine-grained dependency cache.
      fscache:         A file system cacher
      ast_cache:       AST cache to speed up mypy daemon
    """

    def __init__(self, data_dir: str,
                 search_paths: SearchPaths,
                 ignore_prefix: str,
                 source_set: BuildSourceSet,
                 reports: 'Optional[Reports]',
                 options: Options,
                 version_id: str,
                 plugin: Plugin,
                 plugins_snapshot: Dict[str, str],
                 errors: Errors,
                 flush_errors: Callable[[List[str], bool], None],
                 fscache: FileSystemCache,
                 stdout: TextIO,
                 stderr: TextIO,
                 ) -> None:
        self.stats: Dict[str, Any] = {}  # Values are ints or floats
        self.stdout = stdout
        self.stderr = stderr
        self.start_time = time.time()
        self.data_dir = data_dir
        self.errors = errors
        self.errors.set_ignore_prefix(ignore_prefix)
        self.search_paths = search_paths
        self.source_set = source_set
        self.reports = reports
        self.options = options
        self.version_id = version_id
        self.modules: Dict[str, MypyFile] = {}
        self.missing_modules: Set[str] = set()
        self.fg_deps_meta: Dict[str, FgDepMeta] = {}
        # fg_deps holds the dependencies of every module that has been
        # processed. We store this in BuildManager so that we can compute
        # dependencies as we go, which allows us to free ASTs and type information,
        # saving a ton of memory on net.
        self.fg_deps: Dict[str, Set[str]] = {}
        # Always convert the plugin to a ChainedPlugin so that it can be manipulated if needed
        if not isinstance(plugin, ChainedPlugin):
            plugin = ChainedPlugin(options, [plugin])
        self.plugin = plugin
        # Set of namespaces (module or class) that are being populated during semantic
        # analysis and may have missing definitions.
        self.incomplete_namespaces: Set[str] = set()
        self.semantic_analyzer = SemanticAnalyzer(
            self.modules,
            self.missing_modules,
            self.incomplete_namespaces,
            self.errors,
            self.plugin)
        self.all_types: Dict[Expression, Type] = {}  # Enabled by export_types
        self.indirection_detector = TypeIndirectionVisitor()
        self.stale_modules: Set[str] = set()
        self.rechecked_modules: Set[str] = set()
        self.flush_errors = flush_errors
        has_reporters = reports is not None and reports.reporters
        self.cache_enabled = (options.incremental
                              and (not options.fine_grained_incremental
                                   or options.use_fine_grained_cache)
                              and not has_reporters)
        self.fscache = fscache
        self.find_module_cache = FindModuleCache(self.search_paths, self.fscache, self.options)
        self.metastore = create_metastore(options)

        # a mapping from source files to their corresponding shadow files
        # for efficient lookup
        self.shadow_map: Dict[str, str] = {}
        if self.options.shadow_file is not None:
            self.shadow_map = {source_file: shadow_file
                               for (source_file, shadow_file)
                               in self.options.shadow_file}
        # a mapping from each file being typechecked to its possible shadow file
        self.shadow_equivalence_map: Dict[str, Optional[str]] = {}
        self.plugin = plugin
        self.plugins_snapshot = plugins_snapshot
        self.old_plugins_snapshot = read_plugins_snapshot(self)
        self.quickstart_state = read_quickstart_file(options, self.stdout)
        # Fine grained targets (module top levels and top level functions) processed by
        # the semantic analyzer, used only for testing. Currently used only by the new
        # semantic analyzer.
        self.processed_targets: List[str] = []
        # Missing stub packages encountered.
        self.missing_stub_packages: Set[str] = set()
        # Cache for mypy ASTs that have completed semantic analysis
        # pass 1. When multiple files are added to the build in a
        # single daemon increment, only one of the files gets added
        # per step and the others are discarded. This gets repeated
        # until all the files have been added. This means that a
        # new file can be processed O(n**2) times. This cache
        # avoids most of this redundant work.
        self.ast_cache: Dict[str, Tuple[MypyFile, List[ErrorInfo]]] = {}

    def dump_stats(self) -> None:
        if self.options.dump_build_stats:
            print("Stats:")
            for key, value in sorted(self.stats_summary().items()):
                print("{:24}{}".format(key + ":", value))

    def use_fine_grained_cache(self) -> bool:
        return self.cache_enabled and self.options.use_fine_grained_cache

    def maybe_swap_for_shadow_path(self, path: str) -> str:
        if not self.shadow_map:
            return path

        path = normpath(path, self.options)

        previously_checked = path in self.shadow_equivalence_map
        if not previously_checked:
            for source, shadow in self.shadow_map.items():
                if self.fscache.samefile(path, source):
                    self.shadow_equivalence_map[path] = shadow
                    break
                else:
                    self.shadow_equivalence_map[path] = None

        shadow_file = self.shadow_equivalence_map.get(path)
        return shadow_file if shadow_file else path

    def get_stat(self, path: str) -> os.stat_result:
        return self.fscache.stat(self.maybe_swap_for_shadow_path(path))

    def getmtime(self, path: str) -> int:
        """Return a file's mtime; but 0 in bazel mode.

        (Bazel's distributed cache doesn't like filesystem metadata to
        end up in output files.)
        """
        if self.options.bazel:
            return 0
        else:
            return int(self.metastore.getmtime(path))

    def all_imported_modules_in_file(self,
                                     file: MypyFile) -> List[Tuple[int, str, int]]:
        """Find all reachable import statements in a file.

        Return list of tuples (priority, module id, import line number)
        for all modules imported in file; lower numbers == higher priority.

        Can generate blocking errors on bogus relative imports.
        """

        def correct_rel_imp(imp: Union[ImportFrom, ImportAll]) -> str:
            """Function to correct for relative imports."""
            file_id = file.fullname
            rel = imp.relative
            if rel == 0:
                return imp.id
            if os.path.basename(file.path).startswith('__init__.'):
                rel -= 1
            if rel != 0:
                file_id = ".".join(file_id.split(".")[:-rel])
            new_id = file_id + "." + imp.id if imp.id else file_id

            if not new_id:
                self.errors.set_file(file.path, file.name)
                self.errors.report(imp.line, 0,
                                   "No parent module -- cannot perform relative import",
                                   blocker=True)

            return new_id

        res: List[Tuple[int, str, int]] = []
        for imp in file.imports:
            if not imp.is_unreachable:
                if isinstance(imp, Import):
                    pri = import_priority(imp, PRI_MED)
                    ancestor_pri = import_priority(imp, PRI_LOW)
                    for id, _ in imp.ids:
                        # We append the target (e.g. foo.bar.baz)
                        # before the ancestors (e.g. foo and foo.bar)
                        # so that, if FindModuleCache finds the target
                        # module in a package marked with py.typed
                        # underneath a namespace package installed in
                        # site-packages, (gasp), that cache's
                        # knowledge of the ancestors can be primed
                        # when it is asked to find the target.
                        res.append((pri, id, imp.line))
                        ancestor_parts = id.split(".")[:-1]
                        ancestors = []
                        for part in ancestor_parts:
                            ancestors.append(part)
                            res.append((ancestor_pri, ".".join(ancestors), imp.line))
                elif isinstance(imp, ImportFrom):
                    cur_id = correct_rel_imp(imp)
                    all_are_submodules = True
                    # Also add any imported names that are submodules.
                    pri = import_priority(imp, PRI_MED)
                    for name, __ in imp.names:
                        sub_id = cur_id + '.' + name
                        if self.is_module(sub_id):
                            res.append((pri, sub_id, imp.line))
                        else:
                            all_are_submodules = False
                    # Add cur_id as a dependency, even if all of the
                    # imports are submodules. Processing import from will try
                    # to look through cur_id, so we should depend on it.
                    # As a workaround for for some bugs in cycle handling (#4498),
                    # if all of the imports are submodules, do the import at a lower
                    # priority.
                    pri = import_priority(imp, PRI_HIGH if not all_are_submodules else PRI_LOW)
                    # The imported module goes in after the
                    # submodules, for the same namespace related
                    # reasons discussed in the Import case.
                    res.append((pri, cur_id, imp.line))
                elif isinstance(imp, ImportAll):
                    pri = import_priority(imp, PRI_HIGH)
                    res.append((pri, correct_rel_imp(imp), imp.line))

        return res

    def is_module(self, id: str) -> bool:
        """Is there a file in the file system corresponding to module id?"""
        return find_module_simple(id, self) is not None

    def parse_file(self, id: str, path: str, source: str, ignore_errors: bool,
                   options: Options) -> MypyFile:
        """Parse the source of a file with the given name.

        Raise CompileError if there is a parse error.
        """
        t0 = time.time()
        tree = parse(source, path, id, self.errors, options=options)
        tree._fullname = id
        self.add_stats(files_parsed=1,
                       modules_parsed=int(not tree.is_stub),
                       stubs_parsed=int(tree.is_stub),
                       parse_time=time.time() - t0)

        if self.errors.is_blockers():
            self.log("Bailing due to parse errors")
            self.errors.raise_error()

        self.errors.set_file_ignored_lines(path, tree.ignored_lines, ignore_errors)
        return tree

    def load_fine_grained_deps(self, id: str) -> Dict[str, Set[str]]:
        t0 = time.time()
        if id in self.fg_deps_meta:
            # TODO: Assert deps file wasn't changed.
            deps = json.loads(self.metastore.read(self.fg_deps_meta[id]['path']))
        else:
            deps = {}
        val = {k: set(v) for k, v in deps.items()}
        self.add_stats(load_fg_deps_time=time.time() - t0)
        return val

    def report_file(self,
                    file: MypyFile,
                    type_map: Dict[Expression, Type],
                    options: Options) -> None:
        if self.reports is not None and self.source_set.is_source(file):
            self.reports.file(file, self.modules, type_map, options)

    def verbosity(self) -> int:
        return self.options.verbosity

    def log(self, *message: str) -> None:
        if self.verbosity() >= 1:
            if message:
                print('LOG: ', *message, file=self.stderr)
            else:
                print(file=self.stderr)
            self.stderr.flush()

    def log_fine_grained(self, *message: str) -> None:
        import mypy.build
        if self.verbosity() >= 1:
            self.log('fine-grained:', *message)
        elif mypy.build.DEBUG_FINE_GRAINED:
            # Output log in a simplified format that is quick to browse.
            if message:
                print(*message, file=self.stderr)
            else:
                print(file=self.stderr)
            self.stderr.flush()

    def trace(self, *message: str) -> None:
        if self.verbosity() >= 2:
            print('TRACE:', *message, file=self.stderr)
            self.stderr.flush()

    def add_stats(self, **kwds: Any) -> None:
        for key, value in kwds.items():
            if key in self.stats:
                self.stats[key] += value
            else:
                self.stats[key] = value

    def stats_summary(self) -> Mapping[str, object]:
        return self.stats


def deps_to_json(x: Dict[str, Set[str]]) -> str:
    return json.dumps({k: list(v) for k, v in x.items()})


# File for storing metadata about all the fine-grained dependency caches
DEPS_META_FILE: Final = "@deps.meta.json"
# File for storing fine-grained dependencies that didn't a parent in the build
DEPS_ROOT_FILE: Final = "@root.deps.json"

# The name of the fake module used to store fine-grained dependencies that
# have no other place to go.
FAKE_ROOT_MODULE: Final = "@root"


def write_deps_cache(rdeps: Dict[str, Dict[str, Set[str]]],
                     manager: BuildManager, graph: Graph) -> None:
    """Write cache files for fine-grained dependencies.

    Serialize fine-grained dependencies map for fine grained mode.

    Dependencies on some module 'm' is stored in the dependency cache
    file m.deps.json.  This entails some spooky action at a distance:
    if module 'n' depends on 'm', that produces entries in m.deps.json.
    When there is a dependency on a module that does not exist in the
    build, it is stored with its first existing parent module. If no
    such module exists, it is stored with the fake module FAKE_ROOT_MODULE.

    This means that the validity of the fine-grained dependency caches
    are a global property, so we store validity checking information for
    fine-grained dependencies in a global cache file:
     * We take a snapshot of current sources to later check consistency
       between the fine-grained dependency cache and module cache metadata
     * We store the mtime of all of the dependency files to verify they
       haven't changed
    """
    metastore = manager.metastore

    error = False

    fg_deps_meta = manager.fg_deps_meta.copy()

    for id in rdeps:
        if id != FAKE_ROOT_MODULE:
            _, _, deps_json = get_cache_names(id, graph[id].xpath, manager.options)
        else:
            deps_json = DEPS_ROOT_FILE
        assert deps_json
        manager.log("Writing deps cache", deps_json)
        if not manager.metastore.write(deps_json, deps_to_json(rdeps[id])):
            manager.log("Error writing fine-grained deps JSON file {}".format(deps_json))
            error = True
        else:
            fg_deps_meta[id] = {'path': deps_json, 'mtime': manager.getmtime(deps_json)}

    meta_snapshot: Dict[str, str] = {}
    for id, st in graph.items():
        # If we didn't parse a file (so it doesn't have a
        # source_hash), then it must be a module with a fresh cache,
        # so use the hash from that.
        if st.source_hash:
            hash = st.source_hash
        else:
            assert st.meta, "Module must be either parsed or cached"
            hash = st.meta.hash
        meta_snapshot[id] = hash

    meta = {'snapshot': meta_snapshot, 'deps_meta': fg_deps_meta}

    if not metastore.write(DEPS_META_FILE, json.dumps(meta)):
        manager.log("Error writing fine-grained deps meta JSON file {}".format(DEPS_META_FILE))
        error = True

    if error:
        manager.errors.set_file(_cache_dir_prefix(manager.options), None)
        manager.errors.report(0, 0, "Error writing fine-grained dependencies cache",
                              blocker=True)


def invert_deps(deps: Dict[str, Set[str]],
                graph: Graph) -> Dict[str, Dict[str, Set[str]]]:
    """Splits fine-grained dependencies based on the module of the trigger.

    Returns a dictionary from module ids to all dependencies on that
    module. Dependencies not associated with a module in the build will be
    associated with the nearest parent module that is in the build, or the
    fake module FAKE_ROOT_MODULE if none are.
    """
    # Lazy import to speed up startup
    from mypy.server.target import trigger_to_target

    # Prepopulate the map for all the modules that have been processed,
    # so that we always generate files for processed modules (even if
    # there aren't any dependencies to them.)
    rdeps: Dict[str, Dict[str, Set[str]]] = {id: {} for id, st in graph.items() if st.tree}
    for trigger, targets in deps.items():
        module = module_prefix(graph, trigger_to_target(trigger))
        if not module or not graph[module].tree:
            module = FAKE_ROOT_MODULE

        mod_rdeps = rdeps.setdefault(module, {})
        mod_rdeps.setdefault(trigger, set()).update(targets)

    return rdeps


def generate_deps_for_cache(manager: BuildManager,
                            graph: Graph) -> Dict[str, Dict[str, Set[str]]]:
    """Generate fine-grained dependencies into a form suitable for serializing.

    This does a couple things:
    1. Splits fine-grained deps based on the module of the trigger
    2. For each module we generated fine-grained deps for, load any previous
       deps and merge them in.

    Returns a dictionary from module ids to all dependencies on that
    module. Dependencies not associated with a module in the build will be
    associated with the nearest parent module that is in the build, or the
    fake module FAKE_ROOT_MODULE if none are.
    """
    from mypy.server.deps import merge_dependencies  # Lazy import to speed up startup

    # Split the dependencies out into based on the module that is depended on.
    rdeps = invert_deps(manager.fg_deps, graph)

    # We can't just clobber existing dependency information, so we
    # load the deps for every module we've generated new dependencies
    # to and merge the new deps into them.
    for module, mdeps in rdeps.items():
        old_deps = manager.load_fine_grained_deps(module)
        merge_dependencies(old_deps, mdeps)

    return rdeps


PLUGIN_SNAPSHOT_FILE: Final = "@plugins_snapshot.json"


def write_plugins_snapshot(manager: BuildManager) -> None:
    """Write snapshot of versions and hashes of currently active plugins."""
    if not manager.metastore.write(PLUGIN_SNAPSHOT_FILE, json.dumps(manager.plugins_snapshot)):
        manager.errors.set_file(_cache_dir_prefix(manager.options), None)
        manager.errors.report(0, 0, "Error writing plugins snapshot",
                              blocker=True)


def read_plugins_snapshot(manager: BuildManager) -> Optional[Dict[str, str]]:
    """Read cached snapshot of versions and hashes of plugins from previous run."""
    snapshot = _load_json_file(PLUGIN_SNAPSHOT_FILE, manager,
                               log_success='Plugins snapshot ',
                               log_error='Could not load plugins snapshot: ')
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        manager.log('Could not load plugins snapshot: cache is not a dict: {}'
                    .format(type(snapshot)))
        return None
    return snapshot


def read_quickstart_file(options: Options,
                         stdout: TextIO,
                         ) -> Optional[Dict[str, Tuple[float, int, str]]]:
    quickstart: Optional[Dict[str, Tuple[float, int, str]]] = None
    if options.quickstart_file:
        # This is very "best effort". If the file is missing or malformed,
        # just ignore it.
        raw_quickstart: Dict[str, Any] = {}
        try:
            with open(options.quickstart_file, "r") as f:
                raw_quickstart = json.load(f)

            quickstart = {}
            for file, (x, y, z) in raw_quickstart.items():
                quickstart[file] = (x, y, z)
        except Exception as e:
            print("Warning: Failed to load quickstart file: {}\n".format(str(e)), file=stdout)
    return quickstart


def read_deps_cache(manager: BuildManager,
                    graph: Graph) -> Optional[Dict[str, FgDepMeta]]:
    """Read and validate the fine-grained dependencies cache.

    See the write_deps_cache documentation for more information on
    the details of the cache.

    Returns None if the cache was invalid in some way.
    """
    deps_meta = _load_json_file(DEPS_META_FILE, manager,
                                log_success='Deps meta ',
                                log_error='Could not load fine-grained dependency metadata: ')
    if deps_meta is None:
        return None
    meta_snapshot = deps_meta['snapshot']
    # Take a snapshot of the source hashes from all of the metas we found.
    # (Including the ones we rejected because they were out of date.)
    # We use this to verify that they match up with the proto_deps.
    current_meta_snapshot = {id: st.meta_source_hash for id, st in graph.items()
                             if st.meta_source_hash is not None}

    common = set(meta_snapshot.keys()) & set(current_meta_snapshot.keys())
    if any(meta_snapshot[id] != current_meta_snapshot[id] for id in common):
        # TODO: invalidate also if options changed (like --strict-optional)?
        manager.log('Fine-grained dependencies cache inconsistent, ignoring')
        return None

    module_deps_metas = deps_meta['deps_meta']
    if not manager.options.skip_cache_mtime_checks:
        for id, meta in module_deps_metas.items():
            try:
                matched = manager.getmtime(meta['path']) == meta['mtime']
            except FileNotFoundError:
                matched = False
            if not matched:
                manager.log('Invalid or missing fine-grained deps cache: {}'.format(meta['path']))
                return None

    return module_deps_metas


def _load_json_file(file: str, manager: BuildManager,
                    log_success: str, log_error: str) -> Optional[Dict[str, Any]]:
    """A simple helper to read a JSON file with logging."""
    t0 = time.time()
    try:
        data = manager.metastore.read(file)
    except IOError:
        manager.log(log_error + file)
        return None
    manager.add_stats(metastore_read_time=time.time() - t0)
    # Only bother to compute the log message if we are logging it, since it could be big
    if manager.verbosity() >= 2:
        manager.trace(log_success + data.rstrip())
    try:
        t1 = time.time()
        result = json.loads(data)
        manager.add_stats(data_json_load_time=time.time() - t1)
    except json.JSONDecodeError:
        manager.errors.set_file(file, None)
        manager.errors.report(-1, -1,
                              "Error reading JSON file;"
                              " you likely have a bad cache.\n"
                              "Try removing the {cache_dir} directory"
                              " and run mypy again.".format(
                                  cache_dir=manager.options.cache_dir
                              ),
                              blocker=True)
        return None
    else:
        return result


def _cache_dir_prefix(options: Options) -> str:
    """Get current cache directory (or file if id is given)."""
    if options.bazel:
        # This is needed so the cache map works.
        return os.curdir
    cache_dir = options.cache_dir
    pyversion = options.python_version
    base = os.path.join(cache_dir, '%d.%d' % pyversion)
    return base


def add_catch_all_gitignore(target_dir: str) -> None:
    """Add catch-all .gitignore to an existing directory.

    No-op if the .gitignore already exists.
    """
    gitignore = os.path.join(target_dir, ".gitignore")
    try:
        with open(gitignore, "x") as f:
            print("# Automatically created by mypy", file=f)
            print("*", file=f)
    except FileExistsError:
        pass


def exclude_from_backups(target_dir: str) -> None:
    """Exclude the directory from various archives and backups supporting CACHEDIR.TAG.

    If the CACHEDIR.TAG file exists the function is a no-op.
    """
    cachedir_tag = os.path.join(target_dir, "CACHEDIR.TAG")
    try:
        with open(cachedir_tag, "x") as f:
            f.write("""Signature: 8a477f597d28d172789f06886806bc55
# This file is a cache directory tag automatically created by mypy.
# For information about cache directory tags see https://bford.info/cachedir/
""")
    except FileExistsError:
        pass


def create_metastore(options: Options) -> MetadataStore:
    """Create the appropriate metadata store."""
    if options.sqlite_cache:
        mds: MetadataStore = SqliteMetadataStore(_cache_dir_prefix(options))
    else:
        mds = FilesystemMetadataStore(_cache_dir_prefix(options))
    return mds


def get_cache_names(id: str, path: str, options: Options) -> Tuple[str, str, Optional[str]]:
    """Return the file names for the cache files.

    Args:
      id: module ID
      path: module path
      cache_dir: cache directory
      pyversion: Python version (major, minor)

    Returns:
      A tuple with the file names to be used for the meta JSON, the
      data JSON, and the fine-grained deps JSON, respectively.
    """
    if options.cache_map:
        pair = options.cache_map.get(normpath(path, options))
    else:
        pair = None
    if pair is not None:
        # The cache map paths were specified relative to the base directory,
        # but the filesystem metastore APIs operates relative to the cache
        # prefix directory.
        # Solve this by rewriting the paths as relative to the root dir.
        # This only makes sense when using the filesystem backed cache.
        root = _cache_dir_prefix(options)
        return (os.path.relpath(pair[0], root), os.path.relpath(pair[1], root), None)
    prefix = os.path.join(*id.split('.'))
    is_package = os.path.basename(path).startswith('__init__.py')
    if is_package:
        prefix = os.path.join(prefix, '__init__')

    deps_json = None
    if options.cache_fine_grained:
        deps_json = prefix + '.deps.json'
    return (prefix + '.meta.json', prefix + '.data.json', deps_json)


def find_cache_meta(id: str, path: str, manager: BuildManager) -> Optional[CacheMeta]:
    """Find cache data for a module.

    Args:
      id: module ID
      path: module path
      manager: the build manager (for pyversion, log/trace, and build options)

    Returns:
      A CacheMeta instance if the cache data was found and appears
      valid; otherwise None.
    """
    # TODO: May need to take more build options into account
    meta_json, data_json, _ = get_cache_names(id, path, manager.options)
    manager.trace('Looking for {} at {}'.format(id, meta_json))
    t0 = time.time()
    meta = _load_json_file(meta_json, manager,
                           log_success='Meta {} '.format(id),
                           log_error='Could not load cache for {}: '.format(id))
    t1 = time.time()
    if meta is None:
        return None
    if not isinstance(meta, dict):
        manager.log('Could not load cache for {}: meta cache is not a dict: {}'
                    .format(id, repr(meta)))
        return None
    m = cache_meta_from_dict(meta, data_json)
    t2 = time.time()
    manager.add_stats(load_meta_time=t2 - t0,
                      load_meta_load_time=t1 - t0,
                      load_meta_from_dict_time=t2 - t1)

    # Don't check for path match, that is dealt with in validate_meta().
    if (m.id != id or
            m.mtime is None or m.size is None or
            m.dependencies is None or m.data_mtime is None):
        manager.log('Metadata abandoned for {}: attributes are missing'.format(id))
        return None

    # Ignore cache if generated by an older mypy version.
    if ((m.version_id != manager.version_id and not manager.options.skip_version_check)
            or m.options is None
            or len(m.dependencies) + len(m.suppressed) != len(m.dep_prios)
            or len(m.dependencies) + len(m.suppressed) != len(m.dep_lines)):
        manager.log('Metadata abandoned for {}: new attributes are missing'.format(id))
        return None

    # Ignore cache if (relevant) options aren't the same.
    # Note that it's fine to mutilate cached_options since it's only used here.
    cached_options = m.options
    current_options = manager.options.clone_for_module(id).select_options_affecting_cache()
    if manager.options.skip_version_check:
        # When we're lax about version we're also lax about platform.
        cached_options['platform'] = current_options['platform']
    if 'debug_cache' in cached_options:
        # Older versions included debug_cache, but it's silly to compare it.
        del cached_options['debug_cache']
    if cached_options != current_options:
        manager.log('Metadata abandoned for {}: options differ'.format(id))
        if manager.options.verbosity >= 2:
            for key in sorted(set(cached_options) | set(current_options)):
                if cached_options.get(key) != current_options.get(key):
                    manager.trace('    {}: {} != {}'
                                  .format(key, cached_options.get(key), current_options.get(key)))
        return None
    if manager.old_plugins_snapshot and manager.plugins_snapshot:
        # Check if plugins are still the same.
        if manager.plugins_snapshot != manager.old_plugins_snapshot:
            manager.log('Metadata abandoned for {}: plugins differ'.format(id))
            return None
    # So that plugins can return data with tuples in it without
    # things silently always invalidating modules, we round-trip
    # the config data. This isn't beautiful.
    plugin_data = json.loads(json.dumps(
        manager.plugin.report_config_data(ReportConfigContext(id, path, is_check=True))
    ))
    if m.plugin_data != plugin_data:
        manager.log('Metadata abandoned for {}: plugin configuration differs'.format(id))
        return None

    manager.add_stats(fresh_metas=1)
    return m


def validate_meta(meta: Optional[CacheMeta], id: str, path: Optional[str],
                  ignore_all: bool, manager: BuildManager) -> Optional[CacheMeta]:
    '''Checks whether the cached AST of this module can be used.

    Returns:
      None, if the cached AST is unusable.
      Original meta, if mtime/size matched.
      Meta with mtime updated to match source file, if hash/size matched but mtime/path didn't.
    '''
    # This requires two steps. The first one is obvious: we check that the module source file
    # contents is the same as it was when the cache data file was created. The second one is not
    # too obvious: we check that the cache data file mtime has not changed; it is needed because
    # we use cache data file mtime to propagate information about changes in the dependencies.

    if meta is None:
        manager.log('Metadata not found for {}'.format(id))
        return None

    if meta.ignore_all and not ignore_all:
        manager.log('Metadata abandoned for {}: errors were previously ignored'.format(id))
        return None

    t0 = time.time()
    bazel = manager.options.bazel
    assert path is not None, "Internal error: meta was provided without a path"
    if not manager.options.skip_cache_mtime_checks:
        # Check data_json; assume if its mtime matches it's good.
        try:
            data_mtime = manager.getmtime(meta.data_json)
        except OSError:
            manager.log('Metadata abandoned for {}: failed to stat data_json'.format(id))
            return None
        if data_mtime != meta.data_mtime:
            manager.log('Metadata abandoned for {}: data cache is modified'.format(id))
            return None

    if bazel:
        # Normalize path under bazel to make sure it isn't absolute
        path = normpath(path, manager.options)
    try:
        st = manager.get_stat(path)
    except OSError:
        return None
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        manager.log('Metadata abandoned for {}: file {} does not exist'.format(id, path))
        return None

    manager.add_stats(validate_stat_time=time.time() - t0)

    # When we are using a fine-grained cache, we want our initial
    # build() to load all of the cache information and then do a
    # fine-grained incremental update to catch anything that has
    # changed since the cache was generated. We *don't* want to do a
    # coarse-grained incremental rebuild, so we accept the cache
    # metadata even if it doesn't match the source file.
    #
    # We still *do* the mtime/hash checks, however, to enable
    # fine-grained mode to take advantage of the mtime-updating
    # optimization when mtimes differ but hashes match.  There is
    # essentially no extra time cost to computing the hash here, since
    # it will be cached and will be needed for finding changed files
    # later anyways.
    fine_grained_cache = manager.use_fine_grained_cache()

    size = st.st_size
    # Bazel ensures the cache is valid.
    if size != meta.size and not bazel and not fine_grained_cache:
        manager.log('Metadata abandoned for {}: file {} has different size'.format(id, path))
        return None

    # Bazel ensures the cache is valid.
    mtime = 0 if bazel else int(st.st_mtime)
    if not bazel and (mtime != meta.mtime or path != meta.path):
        if manager.quickstart_state and path in manager.quickstart_state:
            # If the mtime and the size of the file recorded in the quickstart dump matches
            # what we see on disk, we know (assume) that the hash matches the quickstart
            # data as well. If that hash matches the hash in the metadata, then we know
            # the file is up to date even though the mtime is wrong, without needing to hash it.
            qmtime, qsize, qhash = manager.quickstart_state[path]
            if int(qmtime) == mtime and qsize == size and qhash == meta.hash:
                manager.log('Metadata fresh (by quickstart) for {}: file {}'.format(id, path))
                meta = meta._replace(mtime=mtime, path=path)
                return meta

        t0 = time.time()
        try:
            # dir means it is a namespace package
            if stat.S_ISDIR(st.st_mode):
                source_hash = ''
            else:
                source_hash = manager.fscache.hash_digest(path)
        except (OSError, UnicodeDecodeError, DecodeError):
            return None
        manager.add_stats(validate_hash_time=time.time() - t0)
        if source_hash != meta.hash:
            if fine_grained_cache:
                manager.log('Using stale metadata for {}: file {}'.format(id, path))
                return meta
            else:
                manager.log('Metadata abandoned for {}: file {} has different hash'.format(
                    id, path))
                return None
        else:
            t0 = time.time()
            # Optimization: update mtime and path (otherwise, this mismatch will reappear).
            meta = meta._replace(mtime=mtime, path=path)
            # Construct a dict we can pass to json.dumps() (compare to write_cache()).
            meta_dict = {
                'id': id,
                'path': path,
                'mtime': mtime,
                'size': size,
                'hash': source_hash,
                'data_mtime': meta.data_mtime,
                'dependencies': meta.dependencies,
                'suppressed': meta.suppressed,
                'options': (manager.options.clone_for_module(id)
                            .select_options_affecting_cache()),
                'dep_prios': meta.dep_prios,
                'dep_lines': meta.dep_lines,
                'interface_hash': meta.interface_hash,
                'version_id': manager.version_id,
                'ignore_all': meta.ignore_all,
                'plugin_data': meta.plugin_data,
            }
            if manager.options.debug_cache:
                meta_str = json.dumps(meta_dict, indent=2, sort_keys=True)
            else:
                meta_str = json.dumps(meta_dict)
            meta_json, _, _ = get_cache_names(id, path, manager.options)
            manager.log('Updating mtime for {}: file {}, meta {}, mtime {}'
                        .format(id, path, meta_json, meta.mtime))
            t1 = time.time()
            manager.metastore.write(meta_json, meta_str)  # Ignore errors, just an optimization.
            manager.add_stats(validate_update_time=time.time() - t1,
                              validate_munging_time=t1 - t0)
            return meta

    # It's a match on (id, path, size, hash, mtime).
    manager.log('Metadata fresh for {}: file {}'.format(id, path))
    return meta


def compute_hash(text: str) -> str:
    # We use a crypto hash instead of the builtin hash(...) function
    # because the output of hash(...)  can differ between runs due to
    # hash randomization (enabled by default in Python 3.3).  See the
    # note in
    # https://docs.python.org/3/reference/datamodel.html#object.__hash__.
    return hash_digest(text.encode('utf-8'))


def json_dumps(obj: Any, debug_cache: bool) -> str:
    if debug_cache:
        return json.dumps(obj, indent=2, sort_keys=True)
    else:
        return json.dumps(obj, sort_keys=True)


def write_cache(id: str, path: str, tree: MypyFile,
                dependencies: List[str], suppressed: List[str],
                dep_prios: List[int], dep_lines: List[int],
                old_interface_hash: str, source_hash: str,
                ignore_all: bool, manager: BuildManager) -> Tuple[str, Optional[CacheMeta]]:
    """Write cache files for a module.

    Note that this mypy's behavior is still correct when any given
    write_cache() call is replaced with a no-op, so error handling
    code that bails without writing anything is okay.

    Args:
      id: module ID
      path: module path
      tree: the fully checked module data
      dependencies: module IDs on which this module depends
      suppressed: module IDs which were suppressed as dependencies
      dep_prios: priorities (parallel array to dependencies)
      dep_lines: import line locations (parallel array to dependencies)
      old_interface_hash: the hash from the previous version of the data cache file
      source_hash: the hash of the source code
      ignore_all: the ignore_all flag for this module
      manager: the build manager (for pyversion, log/trace)

    Returns:
      A tuple containing the interface hash and CacheMeta
      corresponding to the metadata that was written (the latter may
      be None if the cache could not be written).
    """
    metastore = manager.metastore
    # For Bazel we use relative paths and zero mtimes.
    bazel = manager.options.bazel

    # Obtain file paths.
    meta_json, data_json, _ = get_cache_names(id, path, manager.options)
    manager.log('Writing {} {} {} {}'.format(
        id, path, meta_json, data_json))

    # Update tree.path so that in bazel mode it's made relative (since
    # sometimes paths leak out).
    if bazel:
        tree.path = path

    # Serialize data and analyze interface
    data = tree.serialize()
    data_str = json_dumps(data, manager.options.debug_cache)
    interface_hash = compute_hash(data_str)

    plugin_data = manager.plugin.report_config_data(ReportConfigContext(id, path, is_check=False))

    # Obtain and set up metadata
    try:
        st = manager.get_stat(path)
    except OSError as err:
        manager.log("Cannot get stat for {}: {}".format(path, err))
        # Remove apparently-invalid cache files.
        # (This is purely an optimization.)
        for filename in [data_json, meta_json]:
            try:
                os.remove(filename)
            except OSError:
                pass
        # Still return the interface hash we computed.
        return interface_hash, None

    # Write data cache file, if applicable
    # Note that for Bazel we don't record the data file's mtime.
    if old_interface_hash == interface_hash:
        manager.trace("Interface for {} is unchanged".format(id))
    else:
        manager.trace("Interface for {} has changed".format(id))
        if not metastore.write(data_json, data_str):
            # Most likely the error is the replace() call
            # (see https://github.com/python/mypy/issues/3215).
            manager.log("Error writing data JSON file {}".format(data_json))
            # Let's continue without writing the meta file.  Analysis:
            # If the replace failed, we've changed nothing except left
            # behind an extraneous temporary file; if the replace
            # worked but the getmtime() call failed, the meta file
            # will be considered invalid on the next run because the
            # data_mtime field won't match the data file's mtime.
            # Both have the effect of slowing down the next run a
            # little bit due to an out-of-date cache file.
            return interface_hash, None

    try:
        data_mtime = manager.getmtime(data_json)
    except OSError:
        manager.log("Error in os.stat({!r}), skipping cache write".format(data_json))
        return interface_hash, None

    mtime = 0 if bazel else int(st.st_mtime)
    size = st.st_size
    # Note that the options we store in the cache are the options as
    # specified by the command line/config file and *don't* reflect
    # updates made by inline config directives in the file. This is
    # important, or otherwise the options would never match when
    # verifying the cache.
    options = manager.options.clone_for_module(id)
    assert source_hash is not None
    meta = {'id': id,
            'path': path,
            'mtime': mtime,
            'size': size,
            'hash': source_hash,
            'data_mtime': data_mtime,
            'dependencies': dependencies,
            'suppressed': suppressed,
            'options': options.select_options_affecting_cache(),
            'dep_prios': dep_prios,
            'dep_lines': dep_lines,
            'interface_hash': interface_hash,
            'version_id': manager.version_id,
            'ignore_all': ignore_all,
            'plugin_data': plugin_data,
            }

    # Write meta cache file
    meta_str = json_dumps(meta, manager.options.debug_cache)
    if not metastore.write(meta_json, meta_str):
        # Most likely the error is the replace() call
        # (see https://github.com/python/mypy/issues/3215).
        # The next run will simply find the cache entry out of date.
        manager.log("Error writing meta JSON file {}".format(meta_json))

    return interface_hash, cache_meta_from_dict(meta, data_json)


def delete_cache(id: str, path: str, manager: BuildManager) -> None:
    """Delete cache files for a module.

    The cache files for a module are deleted when mypy finds errors there.
    This avoids inconsistent states with cache files from different mypy runs,
    see #4043 for an example.
    """
    # We don't delete .deps files on errors, since the dependencies
    # are mostly generated from other files and the metadata is
    # tracked separately.
    meta_path, data_path, _ = get_cache_names(id, path, manager.options)
    cache_paths = [meta_path, data_path]
    manager.log('Deleting {} {} {}'.format(id, path, " ".join(x for x in cache_paths if x)))

    for filename in cache_paths:
        try:
            manager.metastore.remove(filename)
        except OSError as e:
            if e.errno != errno.ENOENT:
                manager.log("Error deleting cache file {}: {}".format(filename, e.strerror))


"""Dependency manager.

Design
======

Ideally
-------

A. Collapse cycles (each SCC -- strongly connected component --
   becomes one "supernode").

B. Topologically sort nodes based on dependencies.

C. Process from leaves towards roots.

Wrinkles
--------

a. Need to parse source modules to determine dependencies.

b. Processing order for modules within an SCC.

c. Must order mtimes of files to decide whether to re-process; depends
   on clock never resetting.

d. from P import M; checks filesystem whether module P.M exists in
   filesystem.

e. Race conditions, where somebody modifies a file while we're
   processing. Solved by using a FileSystemCache.


Steps
-----

1. For each explicitly given module find the source file location.

2. For each such module load and check the cache metadata, and decide
   whether it's valid.

3. Now recursively (or iteratively) find dependencies and add those to
   the graph:

   - for cached nodes use the list of dependencies from the cache
     metadata (this will be valid even if we later end up re-parsing
     the same source);

   - for uncached nodes parse the file and process all imports found,
     taking care of (a) above.

Step 3 should also address (d) above.

Once step 3 terminates we have the entire dependency graph, and for
each module we've either loaded the cache metadata or parsed the
source code.  (However, we may still need to parse those modules for
which we have cache metadata but that depend, directly or indirectly,
on at least one module for which the cache metadata is stale.)

Now we can execute steps A-C from the first section.  Finding SCCs for
step A shouldn't be hard; there's a recipe here:
http://code.activestate.com/recipes/578507/.  There's also a plethora
of topsort recipes, e.g. http://code.activestate.com/recipes/577413/.

For single nodes, processing is simple.  If the node was cached, we
deserialize the cache data and fix up cross-references.  Otherwise, we
do semantic analysis followed by type checking.  We also handle (c)
above; if a module has valid cache data *but* any of its
dependencies was processed from source, then the module should be
processed from source.

A relatively simple optimization (outside SCCs) we might do in the
future is as follows: if a node's cache data is valid, but one or more
of its dependencies are out of date so we have to re-parse the node
from source, once we have fully type-checked the node, we can decide
whether its symbol table actually changed compared to the cache data
(by reading the cache data and comparing it to the data we would be
writing).  If there is no change we can declare the node up to date,
and any node that depends (and for which we have cached data, and
whose other dependencies are up to date) on it won't need to be
re-parsed from source.

Import cycles
-------------

Finally we have to decide how to handle (c), import cycles.  Here
we'll need a modified version of the original state machine
(build.py), but we only need to do this per SCC, and we won't have to
deal with changes to the list of nodes while we're processing it.

If all nodes in the SCC have valid cache metadata and all dependencies
outside the SCC are still valid, we can proceed as follows:

  1. Load cache data for all nodes in the SCC.

  2. Fix up cross-references for all nodes in the SCC.

Otherwise, the simplest (but potentially slow) way to proceed is to
invalidate all cache data in the SCC and re-parse all nodes in the SCC
from source.  We can do this as follows:

  1. Parse source for all nodes in the SCC.

  2. Semantic analysis for all nodes in the SCC.

  3. Type check all nodes in the SCC.

(If there are more passes the process is the same -- each pass should
be done for all nodes before starting the next pass for any nodes in
the SCC.)

We could process the nodes in the SCC in any order.  For sentimental
reasons, I've decided to process them in the reverse order in which we
encountered them when originally constructing the graph.  That's how
the old build.py deals with cycles, and at least this reproduces the
previous implementation more accurately.

Can we do better than re-parsing all nodes in the SCC when any of its
dependencies are out of date?  It's doubtful.  The optimization
mentioned at the end of the previous section would require re-parsing
and type-checking a node and then comparing its symbol table to the
cached data; but because the node is part of a cycle we can't
technically type-check it until the semantic analysis of all other
nodes in the cycle has completed.  (This is an important issue because
Dropbox has a very large cycle in production code.  But I'd like to
deal with it later.)

Additional wrinkles
-------------------

During implementation more wrinkles were found.

- When a submodule of a package (e.g. x.y) is encountered, the parent
  package (e.g. x) must also be loaded, but it is not strictly a
  dependency.  See State.add_ancestors() below.
"""


class ModuleNotFound(Exception):
    """Control flow exception to signal that a module was not found."""


class State:
    """The state for a module.

    The source is only used for the -c command line option; in that
    case path is None.  Otherwise source is None and path isn't.
    """

    manager: BuildManager
    order_counter: ClassVar[int] = 0
    order: int  # Order in which modules were encountered
    id: str  # Fully qualified module name
    path: Optional[str] = None  # Path to module source
    abspath: Optional[str] = None  # Absolute path to module source
    xpath: str  # Path or '<string>'
    source: Optional[str] = None  # Module source code
    source_hash: Optional[str] = None  # Hash calculated based on the source code
    meta_source_hash: Optional[str] = None  # Hash of the source given in the meta, if any
    meta: Optional[CacheMeta] = None
    data: Optional[str] = None
    tree: Optional[MypyFile] = None
    # We keep both a list and set of dependencies. A set because it makes it efficient to
    # prevent duplicates and the list because I am afraid of changing the order of
    # iteration over dependencies.
    # They should be managed with add_dependency and suppress_dependency.
    dependencies: List[str]  # Modules directly imported by the module
    dependencies_set: Set[str]  # The same but as a set for deduplication purposes
    suppressed: List[str]  # Suppressed/missing dependencies
    suppressed_set: Set[str]  # Suppressed/missing dependencies
    priorities: Dict[str, int]

    # Map each dependency to the line number where it is first imported
    dep_line_map: Dict[str, int]

    # Parent package, its parent, etc.
    ancestors: Optional[List[str]] = None

    # List of (path, line number) tuples giving context for import
    import_context: List[Tuple[str, int]]

    # The State from which this module was imported, if any
    caller_state: Optional["State"] = None

    # If caller_state is set, the line number in the caller where the import occurred
    caller_line = 0

    # If True, indicate that the public interface of this module is unchanged
    externally_same = True

    # Contains a hash of the public interface in incremental mode
    interface_hash: str = ""

    # Options, specialized for this file
    options: Options

    # Whether to ignore all errors
    ignore_all = False

    # Whether the module has an error or any of its dependencies have one.
    transitive_error = False

    # Errors reported before semantic analysis, to allow fine-grained
    # mode to keep reporting them.
    early_errors: List[ErrorInfo]

    # Type checker used for checking this file.  Use type_checker() for
    # access and to construct this on demand.
    _type_checker: Optional[TypeChecker] = None

    fine_grained_deps_loaded = False

    def __init__(self,
                 id: Optional[str],
                 path: Optional[str],
                 source: Optional[str],
                 manager: BuildManager,
                 caller_state: 'Optional[State]' = None,
                 caller_line: int = 0,
                 ancestor_for: 'Optional[State]' = None,
                 root_source: bool = False,
                 # If `temporary` is True, this State is being created to just
                 # quickly parse/load the tree, without an intention to further
                 # process it. With this flag, any changes to external state as well
                 # as error reporting should be avoided.
                 temporary: bool = False,
                 ) -> None:
        if not temporary:
            assert id or path or source is not None, "Neither id, path nor source given"
        self.manager = manager
        State.order_counter += 1
        self.order = State.order_counter
        self.caller_state = caller_state
        self.caller_line = caller_line
        if caller_state:
            self.import_context = caller_state.import_context[:]
            self.import_context.append((caller_state.xpath, caller_line))
        else:
            self.import_context = []
        self.id = id or '__main__'
        self.options = manager.options.clone_for_module(self.id)
        self.early_errors = []
        self._type_checker = None
        if not path and source is None:
            assert id is not None
            try:
                path, follow_imports = find_module_and_diagnose(
                    manager, id, self.options, caller_state, caller_line,
                    ancestor_for, root_source, skip_diagnose=temporary)
            except ModuleNotFound:
                if not temporary:
                    manager.missing_modules.add(id)
                raise
            if follow_imports == 'silent':
                self.ignore_all = True
        self.path = path
        if path:
            self.abspath = os.path.abspath(path)
        self.xpath = path or '<string>'
        if path and source is None and self.manager.cache_enabled:
            self.meta = find_cache_meta(self.id, path, manager)
            # TODO: Get mtime if not cached.
            if self.meta is not None:
                self.interface_hash = self.meta.interface_hash
                self.meta_source_hash = self.meta.hash
        if path and source is None and self.manager.fscache.isdir(path):
            source = ''
        self.source = source
        self.add_ancestors()
        t0 = time.time()
        self.meta = validate_meta(self.meta, self.id, self.path, self.ignore_all, manager)
        self.manager.add_stats(validate_meta_time=time.time() - t0)
        if self.meta:
            # Make copies, since we may modify these and want to
            # compare them to the originals later.
            self.dependencies = list(self.meta.dependencies)
            self.dependencies_set = set(self.dependencies)
            self.suppressed = list(self.meta.suppressed)
            self.suppressed_set = set(self.suppressed)
            all_deps = self.dependencies + self.suppressed
            assert len(all_deps) == len(self.meta.dep_prios)
            self.priorities = {id: pri
                               for id, pri in zip(all_deps, self.meta.dep_prios)}
            assert len(all_deps) == len(self.meta.dep_lines)
            self.dep_line_map = {id: line
                                 for id, line in zip(all_deps, self.meta.dep_lines)}
            if temporary:
                self.load_tree(temporary=True)
            if not manager.use_fine_grained_cache():
                # Special case: if there were a previously missing package imported here
                # and it is not present, then we need to re-calculate dependencies.
                # This is to support patterns like this:
                #     from missing_package import missing_module  # type: ignore
                # At first mypy doesn't know that `missing_module` is a module
                # (it may be a variable, a class, or a function), so it is not added to
                # suppressed dependencies. Therefore, when the package with module is added,
                # we need to re-calculate dependencies.
                # NOTE: see comment below for why we skip this in fine grained mode.
                if exist_added_packages(self.suppressed, manager, self.options):
                    self.parse_file()  # This is safe because the cache is anyway stale.
                    self.compute_dependencies()
        else:
            # When doing a fine-grained cache load, pretend we only
            # know about modules that have cache information and defer
            # handling new modules until the fine-grained update.
            if manager.use_fine_grained_cache():
                manager.log("Deferring module to fine-grained update %s (%s)" % (path, id))
                raise ModuleNotFound

            # Parse the file (and then some) to get the dependencies.
            self.parse_file()
            self.compute_dependencies()

    @property
    def xmeta(self) -> CacheMeta:
        assert self.meta, "missing meta on allegedly fresh module"
        return self.meta

    def add_ancestors(self) -> None:
        if self.path is not None:
            _, name = os.path.split(self.path)
            base, _ = os.path.splitext(name)
            if '.' in base:
                # This is just a weird filename, don't add anything
                self.ancestors = []
                return
        # All parent packages are new ancestors.
        ancestors = []
        parent = self.id
        while '.' in parent:
            parent, _ = parent.rsplit('.', 1)
            ancestors.append(parent)
        self.ancestors = ancestors

    def is_fresh(self) -> bool:
        """Return whether the cache data for this file is fresh."""
        # NOTE: self.dependencies may differ from
        # self.meta.dependencies when a dependency is dropped due to
        # suppression by silent mode.  However when a suppressed
        # dependency is added back we find out later in the process.
        return (self.meta is not None
                and self.is_interface_fresh()
                and self.dependencies == self.meta.dependencies)

    def is_interface_fresh(self) -> bool:
        return self.externally_same

    def mark_as_rechecked(self) -> None:
        """Marks this module as having been fully re-analyzed by the type-checker."""
        self.manager.rechecked_modules.add(self.id)

    def mark_interface_stale(self, *, on_errors: bool = False) -> None:
        """Marks this module as having a stale public interface, and discards the cache data."""
        self.externally_same = False
        if not on_errors:
            self.manager.stale_modules.add(self.id)

    def check_blockers(self) -> None:
        """Raise CompileError if a blocking error is detected."""
        if self.manager.errors.is_blockers():
            self.manager.log("Bailing due to blocking errors")
            self.manager.errors.raise_error()

    @contextlib.contextmanager
    def wrap_context(self, check_blockers: bool = True) -> Iterator[None]:
        """Temporarily change the error import context to match this state.

        Also report an internal error if an unexpected exception was raised
        and raise an exception on a blocking error, unless
        check_blockers is False. Skipping blocking error reporting is used
        in the semantic analyzer so that we can report all blocking errors
        for a file (across multiple targets) to maintain backward
        compatibility.
        """
        save_import_context = self.manager.errors.import_context()
        self.manager.errors.set_import_context(self.import_context)
        try:
            yield
        except CompileError:
            raise
        except Exception as err:
            report_internal_error(err, self.path, 0, self.manager.errors,
                                  self.options, self.manager.stdout, self.manager.stderr)
        self.manager.errors.set_import_context(save_import_context)
        # TODO: Move this away once we've removed the old semantic analyzer?
        if check_blockers:
            self.check_blockers()

    def load_fine_grained_deps(self) -> Dict[str, Set[str]]:
        return self.manager.load_fine_grained_deps(self.id)

    def load_tree(self, temporary: bool = False) -> None:
        assert self.meta is not None, "Internal error: this method must be called only" \
                                      " for cached modules"

        data = _load_json_file(self.meta.data_json, self.manager, "Load tree ",
                               "Could not load tree: ")
        if data is None:
            return None

        t0 = time.time()
        # TODO: Assert data file wasn't changed.
        self.tree = MypyFile.deserialize(data)
        t1 = time.time()
        self.manager.add_stats(deserialize_time=t1 - t0)
        if not temporary:
            self.manager.modules[self.id] = self.tree
            self.manager.add_stats(fresh_trees=1)

    def fix_cross_refs(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        # We need to set allow_missing when doing a fine grained cache
        # load because we need to gracefully handle missing modules.
        fixup_module(self.tree, self.manager.modules,
                     self.options.use_fine_grained_cache)

    # Methods for processing modules from source code.

    def parse_file(self) -> None:
        """Parse file and run first pass of semantic analysis.

        Everything done here is local to the file. Don't depend on imported
        modules in any way. Also record module dependencies based on imports.
        """
        if self.tree is not None:
            # The file was already parsed (in __init__()).
            return

        manager = self.manager

        # Can we reuse a previously parsed AST? This avoids redundant work in daemon.
        cached = self.id in manager.ast_cache
        modules = manager.modules
        if not cached:
            manager.log("Parsing %s (%s)" % (self.xpath, self.id))
        else:
            manager.log("Using cached AST for %s (%s)" % (self.xpath, self.id))

        with self.wrap_context():
            source = self.source
            self.source = None  # We won't need it again.
            if self.path and source is None:
                try:
                    path = manager.maybe_swap_for_shadow_path(self.path)
                    source = decode_python_encoding(manager.fscache.read(path),
                                                    manager.options.python_version)
                    self.source_hash = manager.fscache.hash_digest(path)
                except IOError as ioerr:
                    # ioerr.strerror differs for os.stat failures between Windows and
                    # other systems, but os.strerror(ioerr.errno) does not, so we use that.
                    # (We want the error messages to be platform-independent so that the
                    # tests have predictable output.)
                    raise CompileError([
                        "mypy: can't read file '{}': {}".format(
                            self.path, os.strerror(ioerr.errno))],
                        module_with_blocker=self.id) from ioerr
                except (UnicodeDecodeError, DecodeError) as decodeerr:
                    if self.path.endswith('.pyd'):
                        err = "mypy: stubgen does not support .pyd files: '{}'".format(self.path)
                    else:
                        err = "mypy: can't decode file '{}': {}".format(self.path, str(decodeerr))
                    raise CompileError([err], module_with_blocker=self.id) from decodeerr
            elif self.path and self.manager.fscache.isdir(self.path):
                source = ''
                self.source_hash = ''
            else:
                assert source is not None
                self.source_hash = compute_hash(source)

            self.parse_inline_configuration(source)
            if not cached:
                self.tree = manager.parse_file(self.id, self.xpath, source,
                                               self.ignore_all or self.options.ignore_errors,
                                               self.options)

            else:
                # Reuse a cached AST
                self.tree = manager.ast_cache[self.id][0]
                manager.errors.set_file_ignored_lines(
                    self.xpath,
                    self.tree.ignored_lines,
                    self.ignore_all or self.options.ignore_errors)

        if not cached:
            # Make a copy of any errors produced during parse time so that
            # fine-grained mode can repeat them when the module is
            # reprocessed.
            self.early_errors = list(manager.errors.error_info_map.get(self.xpath, []))
        else:
            self.early_errors = manager.ast_cache[self.id][1]

        modules[self.id] = self.tree

        if not cached:
            self.semantic_analysis_pass1()

        self.check_blockers()

        manager.ast_cache[self.id] = (self.tree, self.early_errors)

    def parse_inline_configuration(self, source: str) -> None:
        """Check for inline mypy: options directive and parse them."""
        flags = get_mypy_comments(source)
        if flags:
            changes, config_errors = parse_mypy_comments(flags, self.options)
            self.options = self.options.apply_changes(changes)
            self.manager.errors.set_file(self.xpath, self.id)
            for lineno, error in config_errors:
                self.manager.errors.report(lineno, 0, error)

    def semantic_analysis_pass1(self) -> None:
        """Perform pass 1 of semantic analysis, which happens immediately after parsing.

        This pass can't assume that any other modules have been processed yet.
        """
        options = self.options
        assert self.tree is not None
        # Do the first pass of semantic analysis: analyze the reachability
        # of blocks and import statements. We must do this before
        # processing imports, since this may mark some import statements as
        # unreachable.
        #
        # TODO: This should not be considered as a semantic analysis
        #     pass -- it's an independent pass.
        analyzer = SemanticAnalyzerPreAnalysis()
        with self.wrap_context():
            analyzer.visit_file(self.tree, self.xpath, self.id, options)
        # TODO: Do this while constructing the AST?
        self.tree.names = SymbolTable()
        if options.allow_redefinition:
            # Perform renaming across the AST to allow variable redefinitions
            self.tree.accept(VariableRenameVisitor())

    def add_dependency(self, dep: str) -> None:
        if dep not in self.dependencies_set:
            self.dependencies.append(dep)
            self.dependencies_set.add(dep)
        if dep in self.suppressed_set:
            self.suppressed.remove(dep)
            self.suppressed_set.remove(dep)

    def suppress_dependency(self, dep: str) -> None:
        if dep in self.dependencies_set:
            self.dependencies.remove(dep)
            self.dependencies_set.remove(dep)
        if dep not in self.suppressed_set:
            self.suppressed.append(dep)
            self.suppressed_set.add(dep)

    def compute_dependencies(self) -> None:
        """Compute a module's dependencies after parsing it.

        This is used when we parse a file that we didn't have
        up-to-date cache information for. When we have an up-to-date
        cache, we just use the cached info.
        """
        manager = self.manager
        assert self.tree is not None

        # Compute (direct) dependencies.
        # Add all direct imports (this is why we needed the first pass).
        # Also keep track of each dependency's source line.
        # Missing dependencies will be moved from dependencies to
        # suppressed when they fail to be loaded in load_graph.

        self.dependencies = []
        self.dependencies_set = set()
        self.suppressed = []
        self.suppressed_set = set()
        self.priorities = {}  # id -> priority
        self.dep_line_map = {}  # id -> line
        dep_entries = (manager.all_imported_modules_in_file(self.tree) +
                       self.manager.plugin.get_additional_deps(self.tree))
        for pri, id, line in dep_entries:
            self.priorities[id] = min(pri, self.priorities.get(id, PRI_ALL))
            if id == self.id:
                continue
            self.add_dependency(id)
            if id not in self.dep_line_map:
                self.dep_line_map[id] = line
        # Every module implicitly depends on builtins.
        if self.id != 'builtins':
            self.add_dependency('builtins')

        self.check_blockers()  # Can fail due to bogus relative imports

    def type_check_first_pass(self) -> None:
        if self.options.semantic_analysis_only:
            return
        with self.wrap_context():
            self.type_checker().check_first_pass()

    def type_checker(self) -> TypeChecker:
        if not self._type_checker:
            assert self.tree is not None, "Internal error: must be called on parsed file only"
            manager = self.manager
            self._type_checker = TypeChecker(manager.errors, manager.modules, self.options,
                                             self.tree, self.xpath, manager.plugin)
        return self._type_checker

    def type_map(self) -> Dict[Expression, Type]:
        return self.type_checker().type_map

    def type_check_second_pass(self) -> bool:
        if self.options.semantic_analysis_only:
            return False
        with self.wrap_context():
            return self.type_checker().check_second_pass()

    def finish_passes(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        manager = self.manager
        if self.options.semantic_analysis_only:
            return
        with self.wrap_context():
            # Some tests (and tools) want to look at the set of all types.
            options = manager.options
            if options.export_types:
                manager.all_types.update(self.type_map())

            # We should always patch indirect dependencies, even in full (non-incremental) builds,
            # because the cache still may be written, and it must be correct.
            self._patch_indirect_dependencies(self.type_checker().module_refs, self.type_map())

            if self.options.dump_inference_stats:
                dump_type_stats(self.tree,
                                self.xpath,
                                modules=self.manager.modules,
                                inferred=True,
                                typemap=self.type_map())
            manager.report_file(self.tree, self.type_map(), self.options)

            self.update_fine_grained_deps(self.manager.fg_deps)
            self.free_state()
            if not manager.options.fine_grained_incremental and not manager.options.preserve_asts:
                free_tree(self.tree)

    def free_state(self) -> None:
        if self._type_checker:
            self._type_checker.reset()
            self._type_checker = None

    def _patch_indirect_dependencies(self,
                                     module_refs: Set[str],
                                     type_map: Dict[Expression, Type]) -> None:
        types = set(type_map.values())
        assert None not in types
        valid = self.valid_references()

        encountered = self.manager.indirection_detector.find_modules(types) | module_refs
        extra = encountered - valid

        for dep in sorted(extra):
            if dep not in self.manager.modules:
                continue
            if dep not in self.suppressed_set and dep not in self.manager.missing_modules:
                self.add_dependency(dep)
                self.priorities[dep] = PRI_INDIRECT
            elif dep not in self.suppressed_set and dep in self.manager.missing_modules:
                self.suppress_dependency(dep)

    def compute_fine_grained_deps(self) -> Dict[str, Set[str]]:
        assert self.tree is not None
        if self.id in ('builtins', 'typing', 'types', 'sys', '_typeshed'):
            # We don't track changes to core parts of typeshed -- the
            # assumption is that they are only changed as part of mypy
            # updates, which will invalidate everything anyway. These
            # will always be processed in the initial non-fine-grained
            # build. Other modules may be brought in as a result of an
            # fine-grained increment, and we may need these
            # dependencies then to handle cyclic imports.
            return {}
        from mypy.server.deps import get_dependencies  # Lazy import to speed up startup
        return get_dependencies(target=self.tree,
                                type_map=self.type_map(),
                                python_version=self.options.python_version,
                                options=self.manager.options)

    def update_fine_grained_deps(self, deps: Dict[str, Set[str]]) -> None:
        options = self.manager.options
        if options.cache_fine_grained or options.fine_grained_incremental:
            from mypy.server.deps import merge_dependencies  # Lazy import to speed up startup
            merge_dependencies(self.compute_fine_grained_deps(), deps)
            TypeState.update_protocol_deps(deps)

    def valid_references(self) -> Set[str]:
        assert self.ancestors is not None
        valid_refs = set(self.dependencies + self.suppressed + self.ancestors)
        valid_refs.add(self.id)

        if "os" in valid_refs:
            valid_refs.add("os.path")

        return valid_refs

    def write_cache(self) -> None:
        assert self.tree is not None, "Internal error: method must be called on parsed file only"
        # We don't support writing cache files in fine-grained incremental mode.
        if (not self.path
                or self.options.cache_dir == os.devnull
                or self.options.fine_grained_incremental):
            return
        is_errors = self.transitive_error
        if is_errors:
            delete_cache(self.id, self.path, self.manager)
            self.meta = None
            self.mark_interface_stale(on_errors=True)
            return
        dep_prios = self.dependency_priorities()
        dep_lines = self.dependency_lines()
        assert self.source_hash is not None
        assert len(set(self.dependencies)) == len(self.dependencies), (
            "Duplicates in dependencies list for {} ({})".format(self.id, self.dependencies))
        new_interface_hash, self.meta = write_cache(
            self.id, self.path, self.tree,
            list(self.dependencies), list(self.suppressed),
            dep_prios, dep_lines, self.interface_hash, self.source_hash, self.ignore_all,
            self.manager)
        if new_interface_hash == self.interface_hash:
            self.manager.log("Cached module {} has same interface".format(self.id))
        else:
            self.manager.log("Cached module {} has changed interface".format(self.id))
            self.mark_interface_stale()
            self.interface_hash = new_interface_hash

    def verify_dependencies(self, suppressed_only: bool = False) -> None:
        """Report errors for import targets in modules that don't exist.

        If suppressed_only is set, only check suppressed dependencies.
        """
        manager = self.manager
        assert self.ancestors is not None
        if suppressed_only:
            all_deps = self.suppressed
        else:
            # Strip out indirect dependencies. See comment in build.load_graph().
            dependencies = [dep for dep in self.dependencies
                            if self.priorities.get(dep) != PRI_INDIRECT]
            all_deps = dependencies + self.suppressed + self.ancestors
        for dep in all_deps:
            if dep in manager.modules:
                continue
            options = manager.options.clone_for_module(dep)
            if options.ignore_missing_imports:
                continue
            line = self.dep_line_map.get(dep, 1)
            try:
                if dep in self.ancestors:
                    state, ancestor = None, self  # type: (Optional[State], Optional[State])
                else:
                    state, ancestor = self, None
                # Called just for its side effects of producing diagnostics.
                find_module_and_diagnose(
                    manager, dep, options,
                    caller_state=state, caller_line=line,
                    ancestor_for=ancestor)
            except (ModuleNotFound, CompileError):
                # Swallow up any ModuleNotFounds or CompilerErrors while generating
                # a diagnostic. CompileErrors may get generated in
                # fine-grained mode when an __init__.py is deleted, if a module
                # that was in that package has targets reprocessed before
                # it is renamed.
                pass

    def dependency_priorities(self) -> List[int]:
        return [self.priorities.get(dep, PRI_HIGH) for dep in self.dependencies + self.suppressed]

    def dependency_lines(self) -> List[int]:
        return [self.dep_line_map.get(dep, 1) for dep in self.dependencies + self.suppressed]

    def generate_unused_ignore_notes(self) -> None:
        if self.options.warn_unused_ignores:
            # If this file was initially loaded from the cache, it may have suppressed
            # dependencies due to imports with ignores on them. We need to generate
            # those errors to avoid spuriously flagging them as unused ignores.
            if self.meta:
                self.verify_dependencies(suppressed_only=True)
            self.manager.errors.generate_unused_ignore_errors(self.xpath)

    def generate_no_code_ignore_notes(self) -> None:
        if self.options.warn_no_ignore_code:
            # If this file was initially loaded from the cache, it may have suppressed
            # dependencies due to imports with ignores on them. We need to generate
            # those errors to avoid spuriously flagging them as unused ignores.
            if self.meta:
                self.verify_dependencies(suppressed_only=True)
            self.manager.errors.generate_no_code_ignore_errors(self.xpath)


# Module import and diagnostic glue


def find_module_and_diagnose(manager: BuildManager,
                             id: str,
                             options: Options,
                             caller_state: 'Optional[State]' = None,
                             caller_line: int = 0,
                             ancestor_for: 'Optional[State]' = None,
                             root_source: bool = False,
                             skip_diagnose: bool = False) -> Tuple[str, str]:
    """Find a module by name, respecting follow_imports and producing diagnostics.

    If the module is not found, then the ModuleNotFound exception is raised.

    Args:
      id: module to find
      options: the options for the module being loaded
      caller_state: the state of the importing module, if applicable
      caller_line: the line number of the import
      ancestor_for: the child module this is an ancestor of, if applicable
      root_source: whether this source was specified on the command line
      skip_diagnose: skip any error diagnosis and reporting (but ModuleNotFound is
          still raised if the module is missing)

    The specified value of follow_imports for a module can be overridden
    if the module is specified on the command line or if it is a stub,
    so we compute and return the "effective" follow_imports of the module.

    Returns a tuple containing (file path, target's effective follow_imports setting)
    """
    file_id = id
    if id == 'builtins' and options.python_version[0] == 2:
        # The __builtin__ module is called internally by mypy
        # 'builtins' in Python 2 mode (similar to Python 3),
        # but the stub file is __builtin__.pyi.  The reason is
        # that a lot of code hard-codes 'builtins.x' and it's
        # easier to work it around like this.  It also means
        # that the implementation can mostly ignore the
        # difference and just assume 'builtins' everywhere,
        # which simplifies code.
        file_id = '__builtin__'
    result = find_module_with_reason(file_id, manager)
    if isinstance(result, str):
        # For non-stubs, look at options.follow_imports:
        # - normal (default) -> fully analyze
        # - silent -> analyze but silence errors
        # - skip -> don't analyze, make the type Any
        follow_imports = options.follow_imports
        if (root_source  # Honor top-level modules
                or (not result.endswith('.py')  # Stubs are always normal
                    and not options.follow_imports_for_stubs)  # except when they aren't
                or id in mypy.semanal_main.core_modules):  # core is always normal
            follow_imports = 'normal'
        if skip_diagnose:
            pass
        elif follow_imports == 'silent':
            # Still import it, but silence non-blocker errors.
            manager.log("Silencing %s (%s)" % (result, id))
        elif follow_imports == 'skip' or follow_imports == 'error':
            # In 'error' mode, produce special error messages.
            if id not in manager.missing_modules:
                manager.log("Skipping %s (%s)" % (result, id))
            if follow_imports == 'error':
                if ancestor_for:
                    skipping_ancestor(manager, id, result, ancestor_for)
                else:
                    skipping_module(manager, caller_line, caller_state,
                                    id, result)
            raise ModuleNotFound
        if not manager.options.no_silence_site_packages:
            for dir in manager.search_paths.package_path + manager.search_paths.typeshed_path:
                if is_sub_path(result, dir):
                    # Silence errors in site-package dirs and typeshed
                    follow_imports = 'silent'
        if (id in CORE_BUILTIN_MODULES
                and not is_typeshed_file(result)
                and not is_stub_package_file(result)
                and not options.use_builtins_fixtures
                and not options.custom_typeshed_dir):
            raise CompileError([
                'mypy: "%s" shadows library module "%s"' % (os.path.relpath(result), id),
                'note: A user-defined top-level module with name "%s" is not supported' % id
            ])
        return (result, follow_imports)
    else:
        # Could not find a module.  Typically the reason is a
        # misspelled module name, missing stub, module not in
        # search path or the module has not been installed.

        ignore_missing_imports = options.ignore_missing_imports
        top_level, second_level = get_top_two_prefixes(file_id)
        # Don't honor a global (not per-module) ignore_missing_imports
        # setting for modules that used to have bundled stubs, as
        # otherwise updating mypy can silently result in new false
        # negatives. (Unless there are stubs but they are incomplete.)
        global_ignore_missing_imports = manager.options.ignore_missing_imports
        py_ver = options.python_version[0]
        if ((is_legacy_bundled_package(top_level, py_ver)
                or is_legacy_bundled_package(second_level, py_ver))
                and global_ignore_missing_imports
                and not options.ignore_missing_imports_per_module
                and result is ModuleNotFoundReason.APPROVED_STUBS_NOT_INSTALLED):
            ignore_missing_imports = False

        if skip_diagnose:
            raise ModuleNotFound
        if caller_state:
            if not (ignore_missing_imports or in_partial_package(id, manager)):
                module_not_found(manager, caller_line, caller_state, id, result)
            raise ModuleNotFound
        elif root_source:
            # If we can't find a root source it's always fatal.
            # TODO: This might hide non-fatal errors from
            # root sources processed earlier.
            raise CompileError(["mypy: can't find module '%s'" % id])
        else:
            raise ModuleNotFound


def exist_added_packages(suppressed: List[str],
                         manager: BuildManager, options: Options) -> bool:
    """Find if there are any newly added packages that were previously suppressed.

    Exclude everything not in build for follow-imports=skip.
    """
    for dep in suppressed:
        if dep in manager.source_set.source_modules:
            # We don't need to add any special logic for this. If a module
            # is added to build, importers will be invalidated by normal mechanism.
            continue
        path = find_module_simple(dep, manager)
        if not path:
            continue
        if (options.follow_imports == 'skip' and
                (not path.endswith('.pyi') or options.follow_imports_for_stubs)):
            continue
        if '__init__.py' in path:
            # It is better to have a bit lenient test, this will only slightly reduce
            # performance, while having a too strict test may affect correctness.
            return True
    return False


def find_module_simple(id: str, manager: BuildManager) -> Optional[str]:
    """Find a filesystem path for module `id` or `None` if not found."""
    x = find_module_with_reason(id, manager)
    if isinstance(x, ModuleNotFoundReason):
        return None
    return x


def find_module_with_reason(id: str, manager: BuildManager) -> ModuleSearchResult:
    """Find a filesystem path for module `id` or the reason it can't be found."""
    t0 = time.time()
    x = manager.find_module_cache.find_module(id)
    manager.add_stats(find_module_time=time.time() - t0, find_module_calls=1)
    return x


def in_partial_package(id: str, manager: BuildManager) -> bool:
    """Check if a missing module can potentially be a part of a package.

    This checks if there is any existing parent __init__.pyi stub that
    defines a module-level __getattr__ (a.k.a. partial stub package).
    """
    while '.' in id:
        parent, _ = id.rsplit('.', 1)
        if parent in manager.modules:
            parent_mod: Optional[MypyFile] = manager.modules[parent]
        else:
            # Parent is not in build, try quickly if we can find it.
            try:
                parent_st = State(id=parent, path=None, source=None, manager=manager,
                                  temporary=True)
            except (ModuleNotFound, CompileError):
                parent_mod = None
            else:
                parent_mod = parent_st.tree
        if parent_mod is not None:
            if parent_mod.is_partial_stub_package:
                return True
            else:
                # Bail out soon, complete subpackage found
                return False
        id = parent
    return False


def module_not_found(manager: BuildManager, line: int, caller_state: State,
                     target: str, reason: ModuleNotFoundReason) -> None:
    errors = manager.errors
    save_import_context = errors.import_context()
    errors.set_import_context(caller_state.import_context)
    errors.set_file(caller_state.xpath, caller_state.id)
    if target == 'builtins':
        errors.report(line, 0, "Cannot find 'builtins' module. Typeshed appears broken!",
                      blocker=True)
        errors.raise_error()
    else:
        daemon = manager.options.fine_grained_incremental
        msg, notes = reason.error_message_templates(daemon)
        pyver = '%d.%d' % manager.options.python_version
        errors.report(line, 0, msg.format(module=target, pyver=pyver), code=codes.IMPORT)
        top_level, second_level = get_top_two_prefixes(target)
        if second_level in legacy_bundled_packages:
            top_level = second_level
        for note in notes:
            if '{stub_dist}' in note:
                note = note.format(stub_dist=legacy_bundled_packages[top_level].name)
            errors.report(line, 0, note, severity='note', only_once=True, code=codes.IMPORT)
        if reason is ModuleNotFoundReason.APPROVED_STUBS_NOT_INSTALLED:
            manager.missing_stub_packages.add(legacy_bundled_packages[top_level].name)
    errors.set_import_context(save_import_context)


def skipping_module(manager: BuildManager, line: int, caller_state: Optional[State],
                    id: str, path: str) -> None:
    """Produce an error for an import ignored due to --follow_imports=error"""
    assert caller_state, (id, path)
    save_import_context = manager.errors.import_context()
    manager.errors.set_import_context(caller_state.import_context)
    manager.errors.set_file(caller_state.xpath, caller_state.id)
    manager.errors.report(line, 0,
                          'Import of "%s" ignored' % (id,),
                          severity='error')
    manager.errors.report(line, 0,
                          "(Using --follow-imports=error, module not passed on command line)",
                          severity='note', only_once=True)
    manager.errors.set_import_context(save_import_context)


def skipping_ancestor(manager: BuildManager, id: str, path: str, ancestor_for: 'State') -> None:
    """Produce an error for an ancestor ignored due to --follow_imports=error"""
    # TODO: Read the path (the __init__.py file) and return
    # immediately if it's empty or only contains comments.
    # But beware, some package may be the ancestor of many modules,
    # so we'd need to cache the decision.
    manager.errors.set_import_context([])
    manager.errors.set_file(ancestor_for.xpath, ancestor_for.id)
    manager.errors.report(-1, -1, 'Ancestor package "%s" ignored' % (id,),
                          severity='error', only_once=True)
    manager.errors.report(-1, -1,
                          "(Using --follow-imports=error, submodule passed on command line)",
                          severity='note', only_once=True)


def log_configuration(manager: BuildManager, sources: List[BuildSource]) -> None:
    """Output useful configuration information to LOG and TRACE"""

    manager.log()
    configuration_vars = [
        ("Mypy Version", __version__),
        ("Config File", (manager.options.config_file or "Default")),
        ("Configured Executable", manager.options.python_executable or "None"),
        ("Current Executable", sys.executable),
        ("Cache Dir", manager.options.cache_dir),
        ("Compiled", str(not __file__.endswith(".py"))),
        ("Exclude", manager.options.exclude),
    ]

    for conf_name, conf_value in configuration_vars:
        manager.log("{:24}{}".format(conf_name + ":", conf_value))

    for source in sources:
        manager.log("{:24}{}".format("Found source:", source))

    # Complete list of searched paths can get very long, put them under TRACE
    for path_type, paths in manager.search_paths._asdict().items():
        if not paths:
            manager.trace("No %s" % path_type)
            continue

        manager.trace("%s:" % path_type)

        for pth in paths:
            manager.trace("    %s" % pth)


# The driver


def dispatch(sources: List[BuildSource],
             manager: BuildManager,
             stdout: TextIO,
             ) -> Graph:
    log_configuration(manager, sources)

    t0 = time.time()
    graph = load_graph(sources, manager)

    # This is a kind of unfortunate hack to work around some of fine-grained's
    # fragility: if we have loaded less than 50% of the specified files from
    # cache in fine-grained cache mode, load the graph again honestly.
    # In this case, we just turn the cache off entirely, so we don't need
    # to worry about some files being loaded and some from cache and so
    # that fine-grained mode never *writes* to the cache.
    if manager.use_fine_grained_cache() and len(graph) < 0.50 * len(sources):
        manager.log("Redoing load_graph without cache because too much was missing")
        manager.cache_enabled = False
        graph = load_graph(sources, manager)

    t1 = time.time()
    manager.add_stats(graph_size=len(graph),
                      stubs_found=sum(g.path is not None and g.path.endswith('.pyi')
                                      for g in graph.values()),
                      graph_load_time=(t1 - t0),
                      fm_cache_size=len(manager.find_module_cache.results),
                      )
    if not graph:
        print("Nothing to do?!", file=stdout)
        return graph
    manager.log("Loaded graph with %d nodes (%.3f sec)" % (len(graph), t1 - t0))
    if manager.options.dump_graph:
        dump_graph(graph, stdout)
        return graph

    # Fine grained dependencies that didn't have an associated module in the build
    # are serialized separately, so we read them after we load the graph.
    # We need to read them both for running in daemon mode and if we are generating
    # a fine-grained cache (so that we can properly update them incrementally).
    # The `read_deps_cache` will also validate
    # the deps cache against the loaded individual cache files.
    if manager.options.cache_fine_grained or manager.use_fine_grained_cache():
        t2 = time.time()
        fg_deps_meta = read_deps_cache(manager, graph)
        manager.add_stats(load_fg_deps_time=time.time() - t2)
        if fg_deps_meta is not None:
            manager.fg_deps_meta = fg_deps_meta
        elif manager.stats.get('fresh_metas', 0) > 0:
            # Clear the stats so we don't infinite loop because of positive fresh_metas
            manager.stats.clear()
            # There were some cache files read, but no fine-grained dependencies loaded.
            manager.log("Error reading fine-grained dependencies cache -- aborting cache load")
            manager.cache_enabled = False
            manager.log("Falling back to full run -- reloading graph...")
            return dispatch(sources, manager, stdout)

    # If we are loading a fine-grained incremental mode cache, we
    # don't want to do a real incremental reprocess of the
    # graph---we'll handle it all later.
    if not manager.use_fine_grained_cache():
        process_graph(graph, manager)
        # Update plugins snapshot.
        write_plugins_snapshot(manager)
        manager.old_plugins_snapshot = manager.plugins_snapshot
        if manager.options.cache_fine_grained or manager.options.fine_grained_incremental:
            # If we are running a daemon or are going to write cache for further fine grained use,
            # then we need to collect fine grained protocol dependencies.
            # Since these are a global property of the program, they are calculated after we
            # processed the whole graph.
            TypeState.add_all_protocol_deps(manager.fg_deps)
            if not manager.options.fine_grained_incremental:
                rdeps = generate_deps_for_cache(manager, graph)
                write_deps_cache(rdeps, manager, graph)

    if manager.options.dump_deps:
        # This speeds up startup a little when not using the daemon mode.
        from mypy.server.deps import dump_all_dependencies
        dump_all_dependencies(manager.modules, manager.all_types,
                              manager.options.python_version, manager.options)
    return graph


class NodeInfo:
    """Some info about a node in the graph of SCCs."""

    def __init__(self, index: int, scc: List[str]) -> None:
        self.node_id = "n%d" % index
        self.scc = scc
        self.sizes: Dict[str, int] = {}  # mod -> size in bytes
        self.deps: Dict[str, int] = {}  # node_id -> pri

    def dumps(self) -> str:
        """Convert to JSON string."""
        total_size = sum(self.sizes.values())
        return "[%s, %s, %s,\n     %s,\n     %s]" % (json.dumps(self.node_id),
                                                     json.dumps(total_size),
                                                     json.dumps(self.scc),
                                                     json.dumps(self.sizes),
                                                     json.dumps(self.deps))


def dump_graph(graph: Graph, stdout: Optional[TextIO] = None) -> None:
    """Dump the graph as a JSON string to stdout.

    This copies some of the work by process_graph()
    (sorted_components() and order_ascc()).
    """
    stdout = stdout or sys.stdout
    nodes = []
    sccs = sorted_components(graph)
    for i, ascc in enumerate(sccs):
        scc = order_ascc(graph, ascc)
        node = NodeInfo(i, scc)
        nodes.append(node)
    inv_nodes = {}  # module -> node_id
    for node in nodes:
        for mod in node.scc:
            inv_nodes[mod] = node.node_id
    for node in nodes:
        for mod in node.scc:
            state = graph[mod]
            size = 0
            if state.path:
                try:
                    size = os.path.getsize(state.path)
                except os.error:
                    pass
            node.sizes[mod] = size
            for dep in state.dependencies:
                if dep in state.priorities:
                    pri = state.priorities[dep]
                    if dep in inv_nodes:
                        dep_id = inv_nodes[dep]
                        if (dep_id != node.node_id and
                                (dep_id not in node.deps or pri < node.deps[dep_id])):
                            node.deps[dep_id] = pri
    print("[" + ",\n ".join(node.dumps() for node in nodes) + "\n]", file=stdout)


def load_graph(sources: List[BuildSource], manager: BuildManager,
               old_graph: Optional[Graph] = None,
               new_modules: Optional[List[State]] = None) -> Graph:
    """Given some source files, load the full dependency graph.

    If an old_graph is passed in, it is used as the starting point and
    modified during graph loading.

    If a new_modules is passed in, any modules that are loaded are
    added to the list. This is an argument and not a return value
    so that the caller can access it even if load_graph fails.

    As this may need to parse files, this can raise CompileError in case
    there are syntax errors.
    """

    graph: Graph = old_graph if old_graph is not None else {}

    # The deque is used to implement breadth-first traversal.
    # TODO: Consider whether to go depth-first instead.  This may
    # affect the order in which we process files within import cycles.
    new = new_modules if new_modules is not None else []
    entry_points: Set[str] = set()
    # Seed the graph with the initial root sources.
    for bs in sources:
        try:
            st = State(id=bs.module, path=bs.path, source=bs.text, manager=manager,
                       root_source=True)
        except ModuleNotFound:
            continue
        if st.id in graph:
            manager.errors.set_file(st.xpath, st.id)
            manager.errors.report(
                -1, -1,
                'Duplicate module named "%s" (also at "%s")' % (st.id, graph[st.id].xpath),
                blocker=True,
            )
            manager.errors.report(
                -1, -1,
                "Are you missing an __init__.py? Alternatively, consider using --exclude to "
                "avoid checking one of them.",
                severity='note'
            )

            manager.errors.raise_error()
        graph[st.id] = st
        new.append(st)
        entry_points.add(bs.module)

    # Note: Running this each time could be slow in the daemon. If it's a problem, we
    # can do more work to maintain this incrementally.
    seen_files = {st.abspath: st for st in graph.values() if st.path}

    # Collect dependencies.  We go breadth-first.
    # More nodes might get added to new as we go, but that's fine.
    for st in new:
        assert st.ancestors is not None
        # Strip out indirect dependencies.  These will be dealt with
        # when they show up as direct dependencies, and there's a
        # scenario where they hurt:
        # - Suppose A imports B and B imports C.
        # - Suppose on the next round:
        #   - C is deleted;
        #   - B is updated to remove the dependency on C;
        #   - A is unchanged.
        # - In this case A's cached *direct* dependencies are still valid
        #   (since direct dependencies reflect the imports found in the source)
        #   but A's cached *indirect* dependency on C is wrong.
        dependencies = [dep for dep in st.dependencies if st.priorities.get(dep) != PRI_INDIRECT]
        if not manager.use_fine_grained_cache():
            # TODO: Ideally we could skip here modules that appeared in st.suppressed
            # because they are not in build with `follow-imports=skip`.
            # This way we could avoid overhead of cloning options in `State.__init__()`
            # below to get the option value. This is quite minor performance loss however.
            added = [dep for dep in st.suppressed if find_module_simple(dep, manager)]
        else:
            # During initial loading we don't care about newly added modules,
            # they will be taken care of during fine grained update. See also
            # comment about this in `State.__init__()`.
            added = []
        for dep in st.ancestors + dependencies + st.suppressed:
            ignored = dep in st.suppressed_set and dep not in entry_points
            if ignored and dep not in added:
                manager.missing_modules.add(dep)
            elif dep not in graph:
                try:
                    if dep in st.ancestors:
                        # TODO: Why not 'if dep not in st.dependencies' ?
                        # Ancestors don't have import context.
                        newst = State(id=dep, path=None, source=None, manager=manager,
                                      ancestor_for=st)
                    else:
                        newst = State(id=dep, path=None, source=None, manager=manager,
                                      caller_state=st, caller_line=st.dep_line_map.get(dep, 1))
                except ModuleNotFound:
                    if dep in st.dependencies_set:
                        st.suppress_dependency(dep)
                else:
                    if newst.path:
                        newst_path = os.path.abspath(newst.path)

                        if newst_path in seen_files:
                            manager.errors.report(
                                -1, 0,
                                'Source file found twice under different module names: '
                                '"{}" and "{}"'.format(seen_files[newst_path].id, newst.id),
                                blocker=True,
                            )
                            manager.errors.report(
                                -1, 0,
                                "See https://mypy.readthedocs.io/en/stable/running_mypy.html#mapping-file-paths-to-modules "  # noqa: E501
                                "for more info",
                                severity='note',
                            )
                            manager.errors.raise_error()

                        seen_files[newst_path] = newst

                    assert newst.id not in graph, newst.id
                    graph[newst.id] = newst
                    new.append(newst)
            if dep in graph and dep in st.suppressed_set:
                # Previously suppressed file is now visible
                st.add_dependency(dep)
    manager.plugin.set_modules(manager.modules)
    return graph


def process_graph(graph: Graph, manager: BuildManager) -> None:
    """Process everything in dependency order."""
    sccs = sorted_components(graph)
    manager.log("Found %d SCCs; largest has %d nodes" %
                (len(sccs), max(len(scc) for scc in sccs)))

    fresh_scc_queue: List[List[str]] = []

    # We're processing SCCs from leaves (those without further
    # dependencies) to roots (those from which everything else can be
    # reached).
    for ascc in sccs:
        # Order the SCC's nodes using a heuristic.
        # Note that ascc is a set, and scc is a list.
        scc = order_ascc(graph, ascc)
        # If builtins is in the list, move it last.  (This is a bit of
        # a hack, but it's necessary because the builtins module is
        # part of a small cycle involving at least {builtins, abc,
        # typing}.  Of these, builtins must be processed last or else
        # some builtin objects will be incompletely processed.)
        if 'builtins' in ascc:
            scc.remove('builtins')
            scc.append('builtins')
        if manager.options.verbosity >= 2:
            for id in scc:
                manager.trace("Priorities for %s:" % id,
                              " ".join("%s:%d" % (x, graph[id].priorities[x])
                                       for x in graph[id].dependencies
                                       if x in ascc and x in graph[id].priorities))
        # Because the SCCs are presented in topological sort order, we
        # don't need to look at dependencies recursively for staleness
        # -- the immediate dependencies are sufficient.
        stale_scc = {id for id in scc if not graph[id].is_fresh()}
        fresh = not stale_scc
        deps = set()
        for id in scc:
            deps.update(graph[id].dependencies)
        deps -= ascc
        stale_deps = {id for id in deps if id in graph and not graph[id].is_interface_fresh()}
        fresh = fresh and not stale_deps
        undeps = set()
        if fresh:
            # Check if any dependencies that were suppressed according
            # to the cache have been added back in this run.
            # NOTE: Newly suppressed dependencies are handled by is_fresh().
            for id in scc:
                undeps.update(graph[id].suppressed)
            undeps &= graph.keys()
            if undeps:
                fresh = False
        if fresh:
            # All cache files are fresh.  Check that no dependency's
            # cache file is newer than any scc node's cache file.
            oldest_in_scc = min(graph[id].xmeta.data_mtime for id in scc)
            viable = {id for id in stale_deps if graph[id].meta is not None}
            newest_in_deps = 0 if not viable else max(graph[dep].xmeta.data_mtime
                                                      for dep in viable)
            if manager.options.verbosity >= 3:  # Dump all mtimes for extreme debugging.
                all_ids = sorted(ascc | viable, key=lambda id: graph[id].xmeta.data_mtime)
                for id in all_ids:
                    if id in scc:
                        if graph[id].xmeta.data_mtime < newest_in_deps:
                            key = "*id:"
                        else:
                            key = "id:"
                    else:
                        if graph[id].xmeta.data_mtime > oldest_in_scc:
                            key = "+dep:"
                        else:
                            key = "dep:"
                    manager.trace(" %5s %.0f %s" % (key, graph[id].xmeta.data_mtime, id))
            # If equal, give the benefit of the doubt, due to 1-sec time granularity
            # (on some platforms).
            if oldest_in_scc < newest_in_deps:
                fresh = False
                fresh_msg = "out of date by %.0f seconds" % (newest_in_deps - oldest_in_scc)
            else:
                fresh_msg = "fresh"
        elif undeps:
            fresh_msg = "stale due to changed suppression (%s)" % " ".join(sorted(undeps))
        elif stale_scc:
            fresh_msg = "inherently stale"
            if stale_scc != ascc:
                fresh_msg += " (%s)" % " ".join(sorted(stale_scc))
            if stale_deps:
                fresh_msg += " with stale deps (%s)" % " ".join(sorted(stale_deps))
        else:
            fresh_msg = "stale due to deps (%s)" % " ".join(sorted(stale_deps))

        # Initialize transitive_error for all SCC members from union
        # of transitive_error of dependencies.
        if any(graph[dep].transitive_error for dep in deps if dep in graph):
            for id in scc:
                graph[id].transitive_error = True

        scc_str = " ".join(scc)
        if fresh:
            manager.trace("Queuing %s SCC (%s)" % (fresh_msg, scc_str))
            fresh_scc_queue.append(scc)
        else:
            if len(fresh_scc_queue) > 0:
                manager.log("Processing {} queued fresh SCCs".format(len(fresh_scc_queue)))
                # Defer processing fresh SCCs until we actually run into a stale SCC
                # and need the earlier modules to be loaded.
                #
                # Note that `process_graph` may end with us not having processed every
                # single fresh SCC. This is intentional -- we don't need those modules
                # loaded if there are no more stale SCCs to be rechecked.
                #
                # Also note we shouldn't have to worry about transitive_error here,
                # since modules with transitive errors aren't written to the cache,
                # and if any dependencies were changed, this SCC would be stale.
                # (Also, in quick_and_dirty mode we don't care about transitive errors.)
                #
                # TODO: see if it's possible to determine if we need to process only a
                # _subset_ of the past SCCs instead of having to process them all.
                for prev_scc in fresh_scc_queue:
                    process_fresh_modules(graph, prev_scc, manager)
                fresh_scc_queue = []
            size = len(scc)
            if size == 1:
                manager.log("Processing SCC singleton (%s) as %s" % (scc_str, fresh_msg))
            else:
                manager.log("Processing SCC of size %d (%s) as %s" % (size, scc_str, fresh_msg))
            process_stale_scc(graph, scc, manager)

    sccs_left = len(fresh_scc_queue)
    nodes_left = sum(len(scc) for scc in fresh_scc_queue)
    manager.add_stats(sccs_left=sccs_left, nodes_left=nodes_left)
    if sccs_left:
        manager.log("{} fresh SCCs ({} nodes) left in queue (and will remain unprocessed)"
                    .format(sccs_left, nodes_left))
        manager.trace(str(fresh_scc_queue))
    else:
        manager.log("No fresh SCCs left in queue")


def order_ascc(graph: Graph, ascc: AbstractSet[str], pri_max: int = PRI_ALL) -> List[str]:
    """Come up with the ideal processing order within an SCC.

    Using the priorities assigned by all_imported_modules_in_file(),
    try to reduce the cycle to a DAG, by omitting arcs representing
    dependencies of lower priority.

    In the simplest case, if we have A <--> B where A has a top-level
    "import B" (medium priority) but B only has the reverse "import A"
    inside a function (low priority), we turn the cycle into a DAG by
    dropping the B --> A arc, which leaves only A --> B.

    If all arcs have the same priority, we fall back to sorting by
    reverse global order (the order in which modules were first
    encountered).

    The algorithm is recursive, as follows: when as arcs of different
    priorities are present, drop all arcs of the lowest priority,
    identify SCCs in the resulting graph, and apply the algorithm to
    each SCC thus found.  The recursion is bounded because at each
    recursion the spread in priorities is (at least) one less.

    In practice there are only a few priority levels (less than a
    dozen) and in the worst case we just carry out the same algorithm
    for finding SCCs N times.  Thus the complexity is no worse than
    the complexity of the original SCC-finding algorithm -- see
    strongly_connected_components() below for a reference.
    """
    if len(ascc) == 1:
        return [s for s in ascc]
    pri_spread = set()
    for id in ascc:
        state = graph[id]
        for dep in state.dependencies:
            if dep in ascc:
                pri = state.priorities.get(dep, PRI_HIGH)
                if pri < pri_max:
                    pri_spread.add(pri)
    if len(pri_spread) == 1:
        # Filtered dependencies are uniform -- order by global order.
        return sorted(ascc, key=lambda id: -graph[id].order)
    pri_max = max(pri_spread)
    sccs = sorted_components(graph, ascc, pri_max)
    # The recursion is bounded by the len(pri_spread) check above.
    return [s for ss in sccs for s in order_ascc(graph, ss, pri_max)]


def process_fresh_modules(graph: Graph, modules: List[str], manager: BuildManager) -> None:
    """Process the modules in one group of modules from their cached data.

    This can be used to process an SCC of modules
    This involves loading the tree from JSON and then doing various cleanups.
    """
    t0 = time.time()
    for id in modules:
        graph[id].load_tree()
    t1 = time.time()
    for id in modules:
        graph[id].fix_cross_refs()
    t2 = time.time()
    manager.add_stats(process_fresh_time=t2 - t0, load_tree_time=t1 - t0)


def process_stale_scc(graph: Graph, scc: List[str], manager: BuildManager) -> None:
    """Process the modules in one SCC from source code.

    Exception: If quick_and_dirty is set, use the cache for fresh modules.
    """
    stale = scc
    for id in stale:
        # We may already have parsed the module, or not.
        # If the former, parse_file() is a no-op.
        graph[id].parse_file()
    if 'typing' in scc:
        # For historical reasons we need to manually add typing aliases
        # for built-in generic collections, see docstring of
        # SemanticAnalyzerPass2.add_builtin_aliases for details.
        typing_mod = graph['typing'].tree
        assert typing_mod, "The typing module was not parsed"
    mypy.semanal_main.semantic_analysis_for_scc(graph, scc, manager.errors)

    # Track what modules aren't yet done so we can finish them as soon
    # as possible, saving memory.
    unfinished_modules = set(stale)
    for id in stale:
        graph[id].type_check_first_pass()
        if not graph[id].type_checker().deferred_nodes:
            unfinished_modules.discard(id)
            graph[id].finish_passes()

    while unfinished_modules:
        for id in stale:
            if id not in unfinished_modules:
                continue
            if not graph[id].type_check_second_pass():
                unfinished_modules.discard(id)
                graph[id].finish_passes()
    for id in stale:
        graph[id].generate_unused_ignore_notes()
        graph[id].generate_no_code_ignore_notes()
    if any(manager.errors.is_errors_for_file(graph[id].xpath) for id in stale):
        for id in stale:
            graph[id].transitive_error = True
    for id in stale:
        manager.flush_errors(manager.errors.file_messages(graph[id].xpath), False)
        graph[id].write_cache()
        graph[id].mark_as_rechecked()


def sorted_components(graph: Graph,
                      vertices: Optional[AbstractSet[str]] = None,
                      pri_max: int = PRI_ALL) -> List[AbstractSet[str]]:
    """Return the graph's SCCs, topologically sorted by dependencies.

    The sort order is from leaves (nodes without dependencies) to
    roots (nodes on which no other nodes depend).

    This works for a subset of the full dependency graph too;
    dependencies that aren't present in graph.keys() are ignored.
    """
    # Compute SCCs.
    if vertices is None:
        vertices = set(graph)
    edges = {id: deps_filtered(graph, vertices, id, pri_max) for id in vertices}
    sccs = list(strongly_connected_components(vertices, edges))
    # Topsort.
    sccsmap = {id: frozenset(scc) for scc in sccs for id in scc}
    data: Dict[AbstractSet[str], Set[AbstractSet[str]]] = {}
    for scc in sccs:
        deps: Set[AbstractSet[str]] = set()
        for id in scc:
            deps.update(sccsmap[x] for x in deps_filtered(graph, vertices, id, pri_max))
        data[frozenset(scc)] = deps
    res = []
    for ready in topsort(data):
        # Sort the sets in ready by reversed smallest State.order.  Examples:
        #
        # - If ready is [{x}, {y}], x.order == 1, y.order == 2, we get
        #   [{y}, {x}].
        #
        # - If ready is [{a, b}, {c, d}], a.order == 1, b.order == 3,
        #   c.order == 2, d.order == 4, the sort keys become [1, 2]
        #   and the result is [{c, d}, {a, b}].
        res.extend(sorted(ready,
                          key=lambda scc: -min(graph[id].order for id in scc)))
    return res


def deps_filtered(graph: Graph, vertices: AbstractSet[str], id: str, pri_max: int) -> List[str]:
    """Filter dependencies for id with pri < pri_max."""
    if id not in vertices:
        return []
    state = graph[id]
    return [dep
            for dep in state.dependencies
            if dep in vertices and state.priorities.get(dep, PRI_HIGH) < pri_max]


def strongly_connected_components(vertices: AbstractSet[str],
                                  edges: Dict[str, List[str]]) -> Iterator[Set[str]]:
    """Compute Strongly Connected Components of a directed graph.

    Args:
      vertices: the labels for the vertices
      edges: for each vertex, gives the target vertices of its outgoing edges

    Returns:
      An iterator yielding strongly connected components, each
      represented as a set of vertices.  Each input vertex will occur
      exactly once; vertices not part of a SCC are returned as
      singleton sets.

    From http://code.activestate.com/recipes/578507/.
    """
    identified: Set[str] = set()
    stack: List[str] = []
    index: Dict[str, int] = {}
    boundaries: List[int] = []

    def dfs(v: str) -> Iterator[Set[str]]:
        index[v] = len(stack)
        stack.append(v)
        boundaries.append(index[v])

        for w in edges[v]:
            if w not in index:
                yield from dfs(w)
            elif w not in identified:
                while index[w] < boundaries[-1]:
                    boundaries.pop()

        if boundaries[-1] == index[v]:
            boundaries.pop()
            scc = set(stack[index[v]:])
            del stack[index[v]:]
            identified.update(scc)
            yield scc

    for v in vertices:
        if v not in index:
            yield from dfs(v)


T = TypeVar("T")


def topsort(data: Dict[T, Set[T]]) -> Iterable[Set[T]]:
    """Topological sort.

    Args:
      data: A map from vertices to all vertices that it has an edge
            connecting it to.  NOTE: This data structure
            is modified in place -- for normalization purposes,
            self-dependencies are removed and entries representing
            orphans are added.

    Returns:
      An iterator yielding sets of vertices that have an equivalent
      ordering.

    Example:
      Suppose the input has the following structure:

        {A: {B, C}, B: {D}, C: {D}}

      This is normalized to:

        {A: {B, C}, B: {D}, C: {D}, D: {}}

      The algorithm will yield the following values:

        {D}
        {B, C}
        {A}

    From http://code.activestate.com/recipes/577413/.
    """
    # TODO: Use a faster algorithm?
    for k, v in data.items():
        v.discard(k)  # Ignore self dependencies.
    for item in set.union(*data.values()) - set(data.keys()):
        data[item] = set()
    while True:
        ready = {item for item, dep in data.items() if not dep}
        if not ready:
            break
        yield ready
        data = {item: (dep - ready)
                for item, dep in data.items()
                if item not in ready}
    assert not data, "A cyclic dependency exists amongst %r" % data


def missing_stubs_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, 'missing_stubs')


def record_missing_stub_packages(cache_dir: str, missing_stub_packages: Set[str]) -> None:
    """Write a file containing missing stub packages.

    This allows a subsequent "mypy --install-types" run (without other arguments)
    to install missing stub packages.
    """
    fnam = missing_stubs_file(cache_dir)
    if missing_stub_packages:
        with open(fnam, 'w') as f:
            for pkg in sorted(missing_stub_packages):
                f.write('%s\n' % pkg)
    else:
        if os.path.isfile(fnam):
            os.remove(fnam)
