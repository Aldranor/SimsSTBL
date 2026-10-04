# Bundled compatibility copy of s4py

Source: https://github.com/thequux/s4py
Author: TQ Hirsch (thequux)

The upstream package uses an obsolete setuptools bootstrap that cannot build
with Python 3.12 and newer. This local packaging metadata replaces only that
bootstrap so the existing SimsSTBL imports can be installed in the project venv.
