"""One-off operator commands that run inside core-api's image.

They live in the package, not in ``scripts/``, because the deployed image holds
only the installed package: a file outside it is not in the container a Cloud
Run job runs, so it cannot be executed there.
"""
