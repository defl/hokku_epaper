This is a project where you're writing firmware for an ESP32 that drives an e-ink display.

Hardware
========
- The known facts are in HARDWARE_FACTS.md, though this might be wrong so treat with caution

Coding and compiling
====================
- always git commit firmware code before building and flashing, the comment is a 1 line summary of the change
- never use the ESP32 USB pins for anything, leave them in their original state such that USB always works
- always double check that you didn't create a fast boot loop by accident
- always make sure there is at least a 15 second window before entering into a low power state
- hard_reset after flashing ESP32 automatically
- the python environment to use is in .venv in the same directory as this file
