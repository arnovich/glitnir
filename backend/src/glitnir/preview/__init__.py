"""The renderer's view-model and assets.

It serves and builds what the renderer shows and starts no thread or server.
It is not free of side effects: the editor's ``--export`` command writes the
renderer bundle, and ``snapshot.Fingerprints`` may run git (through
``environment_fingerprint``) to fingerprint the environment.
"""
