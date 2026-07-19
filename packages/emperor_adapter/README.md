# Emperor Simulator adapter

This Python 3.13 package imports the verified art-production metadata from an
`Emperor-Simulator` checkout without modifying it or copying its media files.
It intentionally reads only the documented legacy inputs. In particular,
`image-gen-key.json` and other credential files are never enumerated or read.

## Python API

```python
from emperor_adapter import dry_run, import_to, export_manifest

preview = dry_run("/path/to/Emperor-Simulator")
report = import_to("/path/to/Emperor-Simulator", "/path/to/new-project")
json_text = export_manifest("/path/to/new-project")
ts_text = export_manifest("/path/to/new-project", format="typescript")
```

`import_to` accepts either a project destination (and creates
`.game-assets`) or a path whose final component is already `.game-assets`.
`metadata_root=` is an alias for `destination=`. Repeating an import is
idempotent: unchanged files are not rewritten.

`export_manifest` returns stable, pretty-printed text. Passing `output=` also
writes the result atomically. The default JSON shape is the legacy manifest
array; TypeScript output exports the same array as `assetManifest`.

## CLI

```bash
emperor-adapter dry-run /path/to/Emperor-Simulator
emperor-adapter import /path/to/Emperor-Simulator /path/to/new-project
emperor-adapter export /path/to/new-project --format typescript -o assetManifest.ts
```

All CLI output is JSON or generated manifest text and contains no credentials.

