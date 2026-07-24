"""Shared platform / flashing code used across screen families.

Houses chip-level tooling (ESP32-S3, XRADIOTECH XR872) that is not tied to a
single screen model — imported by the per-screen modules and the web flash
paths, and packaged so it ships in the hokku-server .deb.
"""
