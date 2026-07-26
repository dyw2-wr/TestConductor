# Contributing

Use Python 3.11 and install the development dependencies:

```bash
python -m venv .venv
python -m pip install -r requirements-dev.txt
python manage.py migrate --noinput
```

Before opening a pull request, run:

```bash
python manage.py makemigrations --check --dry-run
python manage.py check
python -m pytest tests -q
python manage.py test apps.test_platform.tests --verbosity 1
```

Keep credentials and generated data out of commits. Changes to an artifact
contract require matching validators, migrations or schema-version handling,
fixtures, and regression tests. Preserve the separation between test intent,
approved test plan, approved execution plan, and execution evidence.
