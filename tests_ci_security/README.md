# Tests with security ramifications

This directory is **STRICTLY for tests of code with security ramifications**. It is **NOT for general tests of GitHub workflows**. Test ordinary workflow changes by running CI normally.

Keep these tests independent of Genesis, GPU libraries, credentials, and external services. These tests run with Python 3.12 and the standard library only:

```bash
python -m unittest discover -s tests_ci_security -v
```
