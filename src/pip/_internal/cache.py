"""Cache Management
"""

# The following comment should be removed at some point in the future.
# mypy: strict-optional=False

import hashlib
import json
import logging
import os

from pip._vendor.packaging.utils import canonicalize_name

from pip._internal.exceptions import InvalidWheelFilename
from pip._internal.models.link import Link
from pip._internal.models.wheel import Wheel
from pip._internal.pep425tags import interpreter_name, interpreter_version
from pip._internal.utils.compat import expanduser
from pip._internal.utils.temp_dir import TempDirectory
from pip._internal.utils.typing import MYPY_CHECK_RUNNING
from pip._internal.utils.urls import path_to_url

if MYPY_CHECK_RUNNING:
    from typing import Optional, Set, List, Any, Dict
    from pip._internal.models.format_control import FormatControl
    from pip._internal.pep425tags import Pep425Tag

logger = logging.getLogger(__name__)


def _hash_dict(d):
    # type: (Dict[str, str]) -> str
    """Return a stable sha224 of a dictionary."""
    s = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha224(s.encode("ascii")).hexdigest()


class Cache(object):
    """An abstract class - provides cache directories for data from links


        :param cache_dir: The root of the cache.
        :param format_control: An object of FormatControl class to limit
            binaries being read from the cache.
        :param allowed_formats: which formats of files the cache should store.
            ('binary' and 'source' are the only allowed values)
    """

    def __init__(self, cache_dir, format_control, allowed_formats):
        # type: (str, FormatControl, Set[str]) -> None
        super(Cache, self).__init__()
        self.cache_dir = expanduser(cache_dir) if cache_dir else None
        self.format_control = format_control
        self.allowed_formats = allowed_formats

        _valid_formats = {"source", "binary"}
        assert self.allowed_formats.union(_valid_formats) == _valid_formats

    def _get_cache_path_parts_legacy(self, link):
        # type: (Link) -> List[str]
        """Get parts of part that must be os.path.joined with cache_dir

        Legacy cache key (pip < 20) for compatibility with older caches.
        """

        # We want to generate an url to use as our cache key, we don't want to
        # just re-use the URL because it might have other items in the fragment
        # and we don't care about those.
        key_parts = [link.url_without_fragment]
        if link.hash_name is not None and link.hash is not None:
            key_parts.append("=".join([link.hash_name, link.hash]))
        key_url = "#".join(key_parts)

        # Encode our key url with sha224, we'll use this because it has similar
        # security properties to sha256, but with a shorter total output (and
        # thus less secure). However the differences don't make a lot of
        # difference for our use case here.
        hashed = hashlib.sha224(key_url.encode()).hexdigest()

        # We want to nest the directories some to prevent having a ton of top
        # level directories where we might run out of sub directories on some
        # FS.
        parts = [hashed[:2], hashed[2:4], hashed[4:6], hashed[6:]]

        return parts

    def _get_cache_path_parts(self, link):
        # type: (Link) -> List[str]
        """Get parts of part that must be os.path.joined with cache_dir
        """

        # We want to generate an url to use as our cache key, we don't want to
        # just re-use the URL because it might have other items in the fragment
        # and we don't care about those.
        key_parts = {"url": link.url_without_fragment}
        if link.hash_name is not None and link.hash is not None:
            key_parts[link.hash_name] = link.hash
        if link.subdirectory_fragment:
            key_parts["subdirectory"] = link.subdirectory_fragment

        # Include interpreter name, major and minor version in cache key
        # to cope with ill-behaved sdists that build a different wheel
        # depending on the python version their setup.py is being run on,
        # and don't encode the difference in compatibility tags.
        # https://github.com/pypa/pip/issues/7296
        key_parts["interpreter_name"] = interpreter_name()
        key_parts["interpreter_version"] = interpreter_version()

        # Encode our key url with sha224, we'll use this because it has similar
        # security properties to sha256, but with a shorter total output (and
        # thus less secure). However the differences don't make a lot of
        # difference for our use case here.
        hashed = _hash_dict(key_parts)

        # We want to nest the directories some to prevent having a ton of top
        # level directories where we might run out of sub directories on some
        # FS.
        parts = [hashed[:2], hashed[2:4], hashed[4:6], hashed[6:]]

        return parts

    def _get_candidates(self, link, canonical_package_name):
        # type: (Link, Optional[str]) -> List[Any]
        can_not_cache = (
            not self.cache_dir or
            not canonical_package_name or
            not link
        )
        if can_not_cache:
            return []

        formats = self.format_control.get_allowed_formats(
            canonical_package_name
        )
        if not self.allowed_formats.intersection(formats):
            return []

        candidates = []
        path = self.get_path_for_link(link)
        if os.path.isdir(path):
            candidates.extend(os.listdir(path))
        # TODO remove legacy path lookup in pip>=21
        legacy_path = self.get_path_for_link_legacy(link)
        if os.path.isdir(legacy_path):
            candidates.extend(os.listdir(legacy_path))
        return candidates

    def get_path_for_link_legacy(self, link):
        # type: (Link) -> str
        raise NotImplementedError()

    def get_path_for_link(self, link):
        # type: (Link) -> str
        """Return a directory to store cached items in for link.
        """
        raise NotImplementedError()

    def get(
        self,
        link,            # type: Link
        package_name,    # type: Optional[str]
        supported_tags,  # type: List[Pep425Tag]
    ):
        # type: (...) -> Link
        """Returns a link to a cached item if it exists, otherwise returns the
        passed link.
        """
        raise NotImplementedError()

    def _link_for_candidate(self, link, candidate):
        # type: (Link, str) -> Link
        root = self.get_path_for_link(link)
        path = os.path.join(root, candidate)

        return Link(path_to_url(path))

    def cleanup(self):
        # type: () -> None
        pass


class SimpleWheelCache(Cache):
    """A cache of wheels for future installs.
    """

    def __init__(self, cache_dir, format_control):
        # type: (str, FormatControl) -> None
        super(SimpleWheelCache, self).__init__(
            cache_dir, format_control, {"binary"}
        )

    def get_path_for_link_legacy(self, link):
        # type: (Link) -> str
        parts = self._get_cache_path_parts_legacy(link)
        return os.path.join(self.cache_dir, "wheels", *parts)

    def get_path_for_link(self, link):
        # type: (Link) -> str
        """Return a directory to store cached wheels for link

        Because there are M wheels for any one sdist, we provide a directory
        to cache them in, and then consult that directory when looking up
        cache hits.

        We only insert things into the cache if they have plausible version
        numbers, so that we don't contaminate the cache with things that were
        not unique. E.g. ./package might have dozens of installs done for it
        and build a version of 0.0...and if we built and cached a wheel, we'd
        end up using the same wheel even if the source has been edited.

        :param link: The link of the sdist for which this will cache wheels.
        """
        parts = self._get_cache_path_parts(link)

        # Store wheels within the root cache_dir
        return os.path.join(self.cache_dir, "wheels", *parts)

    def get(
        self,
        link,            # type: Link
        package_name,    # type: Optional[str]
        supported_tags,  # type: List[Pep425Tag]
    ):
        # type: (...) -> Link
        candidates = []

        if not package_name:
            return link

        canonical_package_name = canonicalize_name(package_name)
        for wheel_name in self._get_candidates(link, canonical_package_name):
            try:
                wheel = Wheel(wheel_name)
            except InvalidWheelFilename:
                continue
            if canonicalize_name(wheel.name) != canonical_package_name:
                logger.debug(
                    "Ignoring cached wheel {} for {} as it "
                    "does not match the expected distribution name {}.".format(
                        wheel_name, link, package_name
                    )
                )
                continue
            if not wheel.supported(supported_tags):
                # Built for a different python/arch/etc
                continue
            candidates.append(
                (wheel.support_index_min(supported_tags), wheel_name)
            )

        if not candidates:
            return link

        return self._link_for_candidate(link, min(candidates)[1])


class EphemWheelCache(SimpleWheelCache):
    """A SimpleWheelCache that creates it's own temporary cache directory
    """

    def __init__(self, format_control):
        # type: (FormatControl) -> None
        self._temp_dir = TempDirectory(kind="ephem-wheel-cache")

        super(EphemWheelCache, self).__init__(
            self._temp_dir.path, format_control
        )

    def cleanup(self):
        # type: () -> None
        self._temp_dir.cleanup()


class WheelCache(Cache):
    """Wraps EphemWheelCache and SimpleWheelCache into a single Cache

    This Cache allows for gracefully degradation, using the ephem wheel cache
    when a certain link is not found in the simple wheel cache first.
    """

    def __init__(self, cache_dir, format_control):
        # type: (str, FormatControl) -> None
        super(WheelCache, self).__init__(
            cache_dir, format_control, {'binary'}
        )
        self._wheel_cache = SimpleWheelCache(cache_dir, format_control)
        self._ephem_cache = EphemWheelCache(format_control)

    def get_path_for_link_legacy(self, link):
        # type: (Link) -> str
        return self._wheel_cache.get_path_for_link_legacy(link)

    def get_path_for_link(self, link):
        # type: (Link) -> str
        return self._wheel_cache.get_path_for_link(link)

    def get_ephem_path_for_link(self, link):
        # type: (Link) -> str
        return self._ephem_cache.get_path_for_link(link)

    def get(
        self,
        link,            # type: Link
        package_name,    # type: Optional[str]
        supported_tags,  # type: List[Pep425Tag]
    ):
        # type: (...) -> Link
        retval = self._wheel_cache.get(
            link=link,
            package_name=package_name,
            supported_tags=supported_tags,
        )
        if retval is not link:
            return retval

        return self._ephem_cache.get(
            link=link,
            package_name=package_name,
            supported_tags=supported_tags,
        )

    def cleanup(self):
        # type: () -> None
        self._wheel_cache.cleanup()
        self._ephem_cache.cleanup()
