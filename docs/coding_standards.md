\# Coding Standards



\# Quant Research Terminal



\## Purpose



This document defines the coding standards for the entire repository.



Every source file must follow these rules.



\---



\# General Principles



Write code that is:



\- correct

\- explicit

\- deterministic

\- readable

\- testable

\- maintainable



Never optimize readability away.



\---



\# Python Version



Target:



Python 3.12



Use modern Python features where appropriate.



\---



\# Typing



Everything should be type annotated.



Use:



\- explicit return types

\- explicit argument types

\- Protocol where suitable

\- Literal only when appropriate



Avoid Any.



If Any is necessary,

document why.



\---



\# Pydantic



Use immutable models whenever possible.



Validation belongs inside models.



Business logic does not.



\---



\# Functions



Functions should:



\- do one thing

\- have descriptive names

\- avoid hidden side effects



Prefer many small functions over one giant function.



\---



\# Classes



Classes should have one responsibility.



Prefer composition over inheritance.



Avoid deep inheritance trees.



\---



\# Modules



Modules should remain cohesive.



If a file exceeds roughly 300–500 lines because it mixes unrelated responsibilities, consider splitting it.



Do not split modules without architectural justification.



\---



\# Imports



Standard library



↓



Third-party



↓



Project imports



Never use wildcard imports.



\---



\# Constants



Avoid magic numbers.



Prefer:



UPPER\_CASE\_CONSTANTS



\---



\# Exceptions



Raise explicit exceptions.



Never silently ignore exceptions.



Never use bare except.



\---



\# Logging



Prefer structured logging.



Never log sensitive information.



\---



\# Comments



Comments should explain:



WHY



not



WHAT



Code should explain what it does.



\---



\# Docstrings



Public classes:



Required.



Public functions:



Required.



Private helpers:



Optional if obvious.



\---



\# Testing



Every public behavior should be tested.



Every bug fix requires:



Regression test.



\---



\# Naming



Classes:



PascalCase



Functions:



snake\_case



Variables:



snake\_case



Constants:



UPPER\_CASE



Private helpers:



\_leading\_underscore



\---



\# Mutability



Prefer immutable data.



Avoid mutable shared state.



\---



\# Time



UTC only.



Never use local time.



\---



\# Decimal



Financial values should use Decimal where precision matters.



Avoid float for monetary calculations.



\---



\# Performance



Never optimize blindly.



Profile first.



\---



\# Ruff



The repository must pass:



python -m ruff format .



python -m ruff check .



\---



\# MyPy



The repository must pass:



python -m mypy src tests



No new type errors should be introduced.



\---



\# Pytest



The repository must pass:



python -m pytest



Every new feature should include tests.



\---



\# CI



Code that fails CI must not be merged.



\---



\# Repository Quality



Every Pull Request should leave the repository in a better state than before.



Small continuous improvements are encouraged.

