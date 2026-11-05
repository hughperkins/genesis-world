# Tests with security ramifications

This directory is **STRICTLY for tests of code with security ramifications**. It is **NOT for general tests of GitHub workflows**. Test ordinary workflow changes by running CI normally.

The stack validator belongs here because it determines which pull requests may supply executable code to the cluster. Its tests check repository restrictions and ensure invalid or unresolved stacks fail rather than being accepted or silently skipped.

Keep these tests independent of Genesis, GPU libraries, credentials, and external services. They run with Python 3.12 and the standard library only:

```bash
python -m unittest discover -s tests_ci -v
```

The `CI Unit Tests` workflow runs this suite on pull requests and pushes to `main`.
