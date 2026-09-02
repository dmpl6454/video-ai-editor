# Kept as a literal so `import video_ai_editor` stays side-effect free
# (config.py loads .env on import). tests/test_version.py pins it to the
# VERSION file — bump both, or the test fails.
__version__ = "0.5.0"
