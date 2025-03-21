# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0


"""Google Cloud Plugin for Genkit."""


def package_name() -> str:
    """Get the package name for the Google Cloud plugin.

    Returns:
        The fully qualified package name as a string.
    """
    return 'genkit.plugins.google_cloud'


__all__ = ['package_name']
