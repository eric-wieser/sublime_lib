import sublime

import posixpath
from collections import OrderedDict
import os
from abc import ABCMeta, abstractmethod

from .vendor.pathlib.pathlib import Path
from ._util.glob import get_glob_matcher


__all__ = ['ResourcePath']


def _abs_parts(path):
    if path.root:
        return (path.drive, path.root) + path.parts[1:]
    else:
        return path.parts


def _file_relative_to(path, base):
    """
    Like Path.relative_to, except:

    - `base` must be a single Path object.
    - The error message is blank.
    - Only a tuple of parts is returned.

    Surprisingly, this is much, much faster.
    """
    child_parts = _abs_parts(path)
    base_parts = _abs_parts(base)

    n = len(base_parts)
    cf = path._flavour.casefold_parts

    if n == 0:
        compare = (path.root or path.drive)
    else:
        compare = cf(child_parts[:n])

    if compare != cf(base_parts):
        return None

    return child_parts[n:]


class ResourceRoot(metaclass=ABCMeta):
    """
    Represents a directory containing packages.
    """
    def __init__(self, root, path):
        self.resource_root = ResourcePath(root)
        self.file_root = Path(path)

    def resource_to_file_path(self, resource_path):
        """
        Given a :class:`ResourcePath`,
        return the corresponding :class:`Path` within this resource root.

        :raise ValueError: if the :class:`ResourcePath` is not within this resource root.
        """
        resource_path = ResourcePath(resource_path)

        parts = resource_path.relative_to(self.resource_root)
        if parts == ():
            return self.file_root
        else:
            return self._package_file_path(*parts)

    def file_to_resource_path(self, file_path):
        """
        Given an absolute :class:`Path`,
        return the corresponging :class:`ResourcePath` within this resource root,
        or ``None`` if there is no such :class:`ResourcePath`.

        :raise ValueError: if the :class:`Path` is relative.
        """
        file_path = wrap_path(file_path)

        if not file_path.is_absolute():
            raise ValueError("Cannot convert a relative file path to a resource path.")

        parts = _file_relative_to(file_path, self.file_root)
        if parts is None:
            return None
        elif parts == ():
            return self.resource_root
        else:
            return self._package_resource_path(*parts)

    @abstractmethod
    def _package_file_path(self, package, *parts):
        """
        Given a package name and zero or more path segments,
        return the corresponding :class:`Path` within this resource root.
        """
        ...

    @abstractmethod
    def _package_resource_path(self, package, *parts):
        """
        Given a package name and zero or more path segments,
        return the corresponding :class:`ResourcePath` within this resource root.
        """
        ...


class DirectoryResourceRoot(ResourceRoot):
    """
    Represents a directory containing unzipped package directories.
    """
    def _package_file_path(self, *parts):
        return self.file_root.joinpath(*parts)

    def _package_resource_path(self, *parts):
        return self.resource_root.joinpath(*parts)


class InstalledResourceRoot(ResourceRoot):
    """
    Represents a directory containing zipped sublime-package files.
    """
    def _package_file_path(self, package, *rest):
        return self.file_root.joinpath(package + '.sublime-package', *rest)

    def _package_resource_path(self, package, *rest):
        package_path = (self.resource_root / package).remove_suffix('.sublime-package')
        return package_path.joinpath(*rest)


def wrap_path(p):
    if isinstance(p, Path):
        return p
    else:
        return Path(p)


_ROOTS = None


def get_roots():
    global _ROOTS
    if _ROOTS is None:
        _ROOTS = [
            DirectoryResourceRoot('Cache', sublime.cache_path()),
            DirectoryResourceRoot('Packages', sublime.packages_path()),
            InstalledResourceRoot('Packages', sublime.installed_packages_path()),
            InstalledResourceRoot('Packages', Path(sublime.executable_path()).parent / 'Packages'),
        ]
    return _ROOTS


class ResourcePath():
    """
    A pathlib-inspired representation of a Sublime Text resource path.

    Resource paths are similar to filesystem paths in many ways,
    yet different in other ways.
    Many features of :class:`pathlib.Path` objects
    are not implemented by :class:`ResourcePath`,
    and other features may have differerent interpretations.

    A resource path consists of one or more parts
    separated by forward slashes (regardless of platform).
    The first part is the root.
    At the present time, the only roots that Sublime uses are
    ``'Packages'`` and ``'Caches'``.
    Resource paths are always absolute;
    dots in resource paths have no special meaning.

    :class:`ResourcePath` objects are immutable and hashable.
    The forward slash operator is a shorthand for :meth:`joinpath`.
    The string representation of a :class:`ResourcePath`
    is the raw resource path in the form that Sublime Text uses.

    Some methods accept glob patterns as arguments.
    Glob patterns are interpreted as in pathlib.
    Recursive globs (**) are always allowed, even in :meth:`match`.
    Leading slashes are not matched literally.
    A pattern with a leading slash must match the entire path
    and not merely a suffix of the path.

    .. versionadded:: 1.2
    """

    @classmethod
    def glob_resources(cls, pattern):
        """
        Find all resources that match the given pattern
        and return them as :class:`ResourcePath` objects.
        """
        match = get_glob_matcher(pattern)
        return [
            cls(path) for path in sublime.find_resources('')
            if match(path)
        ]

    @classmethod
    def from_file_path(cls, file_path):
        """
        Return a :class:`ResourcePath` corresponding to the given file path.

        If the file path corresponds to a resource inside an installed package,
        then return the path to that resource.

        :raise ValueError: if the given file path does not correspond to any resource path.
        :raise ValueError: if the given file path is relative.

        .. code-block:: python

           >>> ResourcePath.from_file_path(
              os.path.join(sublime.packages_path(), 'My Package', 'foo.py')
           )
           ResourcePath("Packages/My Package/foo.py")

           >>> ResourcePath.from_file_path(
              os.path.join(
                sublime.installed_packages_path(),
                'My Package.sublime-package',
                'foo.py'
              )
           )
           ResourcePath("Packages/My Package/foo.py")
        """

        file_path = wrap_path(file_path)
        try:
            return next(
                path
                for path in (
                    root.file_to_resource_path(file_path)
                    for root in get_roots()
                )
                if path is not None
            )
        except StopIteration:
            raise ValueError(
                "Path {!r} does not correspond to any resource path.".format(file_path)
            )

    def __init__(self, *pathsegments):
        """
        Construct a :class:`ResourcePath` object with the given parts.

        :raise ValueError: if the resulting path would be empty.
        """
        first, *rest = pathsegments
        if isinstance(first, ResourcePath):
            self._parts = first.parts + self._parse_segments(rest)
        else:
            self._parts = self._parse_segments(pathsegments)

        if self._parts == ():
            raise ValueError("Empty path.")

    def _parse_segments(self, pathsegments):
        return tuple(
            part
            for segment in pathsegments if segment
            for part in posixpath.normpath(str(segment)).split('/')
        )

    def __hash__(self):
        return hash(self.parts)

    def __repr__(self):
        return "{}({!r})".format(self.__class__.__name__, str(self))

    def __str__(self):
        return '/'.join(self.parts)

    def __eq__(self, other):
        return isinstance(other, ResourcePath) and self._parts == other.parts

    def __truediv__(self, other):
        return self.joinpath(other)

    @property
    def parts(self):
        """
        A tuple giving access to the path’s various components.
        """
        return self._parts

    @property
    def parent(self):
        """
        The logical parent of the path. A root path is its own parent.
        """
        if len(self._parts) == 1:
            return self
        else:
            return self.__class__(*self._parts[:-1])

    @property
    def parents(self):
        """
        An immutable sequence providing access to the path's logical ancestors.
        """
        parent = self.parent
        if self == parent:
            return ()
        else:
            return (parent,) + parent.parents

    @property
    def name(self):
        """
        A string representing the final path component.
        """
        return self._parts[-1]

    @property
    def suffix(self):
        """
        The final component's last suffix, if any.
        """
        name = self.name
        i = name.rfind('.')
        if 0 < i < len(name) - 1:
            return name[i:]
        else:
            return ''

    @property
    def suffixes(self):
        """
        A list of the final component's suffixes, if any.
        """
        name = self.name
        if name.endswith('.'):
            return []
        name = name.lstrip('.')
        return ['.' + suffix for suffix in name.split('.')[1:]]

    @property
    def stem(self):
        """
        The final path component, minus its last suffix.
        """
        name = self.name
        i = name.rfind('.')
        if 0 < i < len(name) - 1:
            return name[:i]
        else:
            return name

    @property
    def root(self):
        """
        The first path component (usually ``'Packages'`` or ``'Cache'``).
        """
        return self._parts[0]

    @property
    def package(self):
        """
        The name of the package the path is within,
        or ``None`` if the path is a root path.
        """
        if len(self._parts) >= 2:
            return self._parts[1]
        else:
            return None

    def match(self, pattern):
        """
        Return ``True`` if this path matches the given glob pattern,
        or ``False`` otherwise.

        :raise ValueError: if `pattern` is invalid.
        """
        match = get_glob_matcher(pattern)
        return match(str(self))

    def joinpath(self, *other):
        """
        Combine this path with all of the given strings.
        """
        return self.__class__(self, *other)

    def relative_to(self, *other):
        """
        Compute a tuple `parts` of path components such that ``self == other.joinpath(*parts)``.

        `other` will be converted to a :class:`ResourcePath`.

        :raise ValueError: if this path is not a descendant of `other`.

        .. versionadded:: 1.3
        """
        other_path = ResourcePath(*other)
        other_len = len(other_path.parts)

        if other_path.parts == self._parts[:other_len]:
            return self._parts[other_len:]
        else:
            raise ValueError("{!s} does not start with {!s}".format(self, other_path))

    def with_name(self, name):
        """
        Return a new path with the name changed.
        """
        if len(self._parts) == 1:
            return self.__class__(name)
        else:
            return self.parent / name

    def add_suffix(self, suffix):
        """
        Return a new path with the suffix added.

        .. versionadded:: 1.3
        """
        return self.with_name(self.name + suffix)

    def remove_suffix(self, suffix=None, *, must_remove=True):
        """
        Return a new path with the suffix removed.

        If `suffix` is ``None`` (the default), then ``self.suffix`` will be removed.
        If `suffix` is a string, then only that suffix will be removed.
        Otherwise, if `suffix` is iterable,
        then the longest possible item in `suffix` will be removed.

        :raise ValueError: if `must_remove` is ``True`` (the default)
            and no suffix can be removed.

        .. versionadded:: 1.3
        """
        new_name = None

        if suffix is None:
            if self.suffix:
                new_name = self.stem
        else:
            if isinstance(suffix, str):
                suffixes = [suffix]
            else:
                suffixes = sorted(suffix, key=len, reverse=True)

            old_name = self.name
            new_name = next((
                old_name[:i]
                for s in suffixes
                for i in (old_name.rfind(s),)
                if i > 0
            ), None)

        if new_name is not None:
            return self.with_name(new_name)
        elif must_remove:
            raise ValueError('Cannot remove suffix {!r} from {!r}.'.format(suffix, self))
        else:
            return self

    def with_suffix(self, suffix):
        """
        Return a new path with the suffix changed.

        If the original path doesn’t have a suffix, the new suffix is appended
        instead. If the new suffix is an empty string, the original suffix is
        removed.

        Equivalent to ``self.remove_suffix(must_remove=False).add_suffix(suffix)``.
        """
        return self.with_name(self.stem + suffix)

    def file_path(self):
        """
        Return a :class:`Path` object representing a filesystem path
        inside one of Sublime's data directories.

        Even if there is a resource at this path,
        there may not be a file at that filesystem path.
        The resource could be in a default package or an installed package.

        :raise ValueError: if the path's root is not used by Sublime.
        """
        for root in get_roots():
            try:
                return root.resource_to_file_path(self)
            except ValueError:
                continue

        raise ValueError("Can't find a filesystem path for {!r}.".format(self.root)) from None

    def exists(self):
        """
        Return ``True`` if there is a resource at this path,
        or ``False`` otherwise.

        The resource system does not keep track of directories.
        Even if a path does not point to a resource,
        there may be resources beneath that path.
        """
        return str(self) in sublime.find_resources(self.name)

    def read_text(self):
        """
        Load the resource at this path and return it as text.

        :raise FileNotFoundError: if there is no resource at this path.

        :raise UnicodeDecodeError: if the resource cannot be decoded as UTF-8.
        """
        try:
            return sublime.load_resource(str(self))
        except IOError as err:
            raise FileNotFoundError(str(self)) from err

    def read_bytes(self):
        """
        Load the resource at this path and return it as bytes.

        :raise FileNotFoundError: if there is no resource at this path.
        """
        try:
            return sublime.load_binary_resource(str(self))
        except IOError as err:
            raise FileNotFoundError(str(self)) from err

    def glob(self, pattern):
        """
        Glob the given pattern at this path, returning all matching resources.

        :raise ValueError: if `pattern` is invalid.
        """
        base = '/' + str(self) + '/' if self._parts else ''
        return ResourcePath.glob_resources(base + pattern)

    def rglob(self, pattern):
        """
        Shorthand for ``path.glob('**/' + pattern)``.

        :raise ValueError: if `pattern` is invalid.

        :raise NotImplementedError: if `pattern` begins with a slash.
        """
        if pattern.startswith('/'):
            raise NotImplementedError("Non-relative patterns are unsupported")

        return self.glob('**/' + pattern)

    def children(self):
        """
        Return a list of paths that are direct children of this path
        and point to a resource at or beneath that path.
        """
        depth = len(self._parts)
        return [
            self / next_part
            for next_part in OrderedDict.fromkeys(
                resource.parts[depth]
                for resource in self.glob('**')
            )
        ]

    def copy(self, target, exist_ok=True):
        """
        Copy this resource to the given `target`.

        `target` should be a string representing a filesystem path
        or a value convertible to string.
        If `target` exists and is a file,
        and `exist_ok` is ``True`` (the default),
        it will be silently replaced.

        :raise FileNotFoundError: if there is no resource at this path.
        :raise IsADirectoryError: if `target` is a directory.
        :raise FileExistsError: if `target` is a file and `exist_ok` is ``False``.

        .. versionadded:: 1.3
        """
        if exist_ok:
            mode = 'w'
        else:
            mode = 'x'

        data = self.read_bytes()
        with open(str(target), mode + 'b') as file:
            file.write(data)

    def copytree(self, target, exist_ok=False):
        """
        Copy all resources beneath this path into a directory tree rooted at `target`.

        All missing parent directories of `target` will be created.

        If `exist_ok` is ``False`` (the default),
        then `target` must not already exist.
        If `exist_ok` is ``True``,
        then existing files under `target` will be overwritten.

        :raise FileExistsError: if `target` already exists and `exist_ok` is ``False``.

        .. versionadded:: 1.3
        """
        target = wrap_path(target)

        os.makedirs(str(target), exist_ok=exist_ok)

        for resource in self.rglob('*'):
            file_path = target.joinpath(*resource.relative_to(self))
            os.makedirs(str(file_path.parent), exist_ok=True)
            resource.copy(file_path)
