# Tests with security ramifications

This directory is **STRICTLY for tests of code with security ramifications**. It is **NOT for general tests of GitHub workflows**. Test ordinary workflow changes by running CI normally.

Keep these tests independent of Genesis, GPU libraries, credentials, and external services. These tests run with Python 3.12 and pytest:

```bash
python -m pytest -c tests_ci_security/pytest.ini tests_ci_security
```
