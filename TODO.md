# TODO

Items captured during development that aren't urgent enough to block current
work but worth picking up later. Add new entries at the top.

- Add a specialized test for the memory_guard, allocate large numpy array and see if it trips.
- Need to take a good look at the threadpool, want it to be fully within image_manager
- Implement a second rendering system using pyvips (need to make the interfaces a bit nicer first before doing that) 
- png can blow up memory with crazy large images on pi