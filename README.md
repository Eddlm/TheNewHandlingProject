# proper-handling

`proper-handling` is a data-only FiveM resource that loads handling overrides from the `data` folder.

## Runtime behavior

- The manifest packages `data/**/*.meta`, but only `data/**/handling_*.meta` is registered as `HANDLING_FILE`.
- In this checkout, that means the active runtime files are:
  - `data/handling_basegame.meta`
  - `data/handling_john_doe.meta`
  - `data/handling_mods.meta`
- The resource has no client scripts, server scripts, commands, events, exports, or convars.

## Important folder detail

Several root-level `.meta` files exist in this folder, including `handling.meta`, `handling_empty.meta`, `handling_smukk.meta`, `handling_stig.meta`, `handling_tidemo.meta`, and `_handling_*.meta` files.

Those files are not loaded by the current `fxmanifest.lua`, because the manifest only targets `data/**/handling_*.meta`.

If one of those root-level files should become active, the manifest needs to be changed explicitly.

## What this resource is for

- Overriding handling data through packaged `.meta` files
- Keeping active runtime data separated from older experiments and reference files

## Notes

- This resource depends only on normal FiveM/GTA handling-meta support.
- The runtime behavior is strictly "load handling data from `data/handling_*.meta`".
