"""Booley — RTL development harness."""

from importlib.metadata import PackageNotFoundError, version

#: Distribution that actually supplied ``__version__``. ``booley-rtl`` is the
#: current PyPI name; ``booley`` is the pre-rename one, which still exists in
#: the wild as a leftover editable install and can shadow a newer checkout
#: (see the ``legacy distribution`` doctor check). ``None`` means neither is
#: installed — an import straight off ``sys.path``, e.g. a source tree run.
__dist_name__: str | None

try:
    # Distribution name on PyPI is "booley-rtl"; the import package is "booley".
    __version__ = version("booley-rtl")
    __dist_name__ = "booley-rtl"
except PackageNotFoundError:
    try:
        # Pre-rename dev/editable installs registered the old distribution name.
        __version__ = version("booley")
        __dist_name__ = "booley"
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"
        __dist_name__ = None
