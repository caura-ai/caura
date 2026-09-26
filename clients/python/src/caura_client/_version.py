"""Package version, kept in its own module so ``client`` can import it
without a circular import through ``__init__``. Must agree with
``pyproject.toml``; the publish workflow checks the tag against that file.
"""

__version__ = "1.0.3"
