# GAZ Mod Deploy — Specification

## 1. Overview

`deploy_target_build.py` is a post-build deployment tool for 7 Days To Die mods.
It copies the just-built assembly (`.dll`, and `.pdb` for Debug) from the MSBuild
output directory to the appropriate runtime location, mirrors the mod's
`ModPackage/` contents into the same location, and (for Release) packages the
result into a versioned ZIP.

The tool is intended to be invoked by MSBuild after a successful build. It
reads most of its configuration from a YAML file (`gzdeploy.yaml`) so the same
deployment script can be shared across multiple mod projects.

## 2. Files

| File | Purpose |
|---|---|
| `deploy_target_build.py` | The deployment script (Python 3, requires `PyYAML` for YAML support). |
| `gzdeploy.yaml` | Per-project (or shared) configuration file. |
| `LICENSE` | MIT license. |

## 3. Command-Line Interface

```
deploy_target_build.py [BuildTarget] [flags]
```

### 3.1 Positional arguments

| Argument | Required | Description |
|---|---|---|
| `BuildTarget` | optional | Build configuration: `Debug` or `Release`. Falls back to `build_target` in `gzdeploy.yaml`. |

### 3.2 Optional flags

| Flag | YAML key | Effect |
|---|---|---|
| `--clean` | `clean` | Wipe the deployment directory before copying. |
| `--force` | `force` | Copy all `ModPackage` files regardless of modification time. |
| `--no-pause` | `no_pause` | Skip the keypress pause that normally follows errors. |
| `--verbose` | `verbose` | Show detailed output for every file operation. |
| `--config FILE` | — | Path to the YAML config file. |

**Precedence rules:**
- `BuildTarget`: CLI positional > `build_target` in YAML.
- `clean` / `force` / `no_pause` / `verbose`: presence of the CLI flag sets the
  value to `True`; if the flag is absent the YAML value is used. There is no
  `--no-clean` / `--no-force` negation flag, so a YAML value of `True` cannot
  be overridden to `False` from the command line.

## 4. Configuration File (`gzdeploy.yaml`)

### 4.1 Discovery order

The first match wins:

1. The file passed via `--config FILE`.
2. `gzdeploy.yaml` in the current working directory (typically the MSBuild project directory).
3. `gzdeploy.yaml` next to `deploy_target_build.py` (shared global fallback).

### 4.2 Keys

| Key | Type | Required | Description |
|---|---|---|---|
| `mod_name` | string | yes | Used for the assembly filename (`<mod_name>.dll` / `.pdb`) and as the deployment folder name. |
| `build_target` | string | yes (or supply CLI) | `Debug` or `Release`. |
| `output_dir` | string | yes | Directory containing the built assemblies. May contain `{build_target}`. |
| `project_dir` | string | yes | Root of the mod project. Must contain `ModPackage/`. |
| `clean` | bool | no (default `false`) | See `--clean`. |
| `force` | bool | no (default `false`) | See `--force`. |
| `no_pause` | bool | no (default `false`) | See `--no-pause`. |
| `verbose` | bool | no (default `false`) | See `--verbose`. |
| `always_copy_masks` | list[string] | no | fnmatch patterns; matching `ModPackage` files are always copied regardless of mtime. |
| `additional_output_files` | list[string] | no | Extra filenames or globs copied from `output_dir` (always overwritten, same rule as DLL/PDB). |
| `additional_files` | list[string] | no | Extra files from any location — absolute paths, or paths relative to `project_dir`. Globs supported. |

### 4.3 `{`key`}` substitution

Any string value in the YAML may reference the four core values using
`{mod_name}`, `{build_target}`, `{output_dir}`, or `{project_dir}`.

- Cross-references between core values are resolved iteratively
  (e.g. `project_dir: "{mod_name}_src"` with `mod_name: MyMod` yields
  `project_dir = "MyMod_src"`).
- Unresolved placeholders (None / missing keys) are left untouched.
- Placeholders are substituted into *all* string values in the config,
  including entries inside `always_copy_masks`, `additional_files`, and
  `additional_output_files`.
- Values containing `{...}` syntax must be quoted in YAML
  (e.g. `project_dir: "{mod_name}_src"`) to prevent the parser from
  treating the braces as flow-mapping syntax.

## 5. Deployment Behaviour

### 5.1 Build targets

| Target | Deployment directory | Assembly files copied | ZIP produced |
|---|---|---|---|
| `Debug` | `%APPDATA%/7DaysToDie/Mods/Dev_<mod_name>` | `<mod_name>.dll`, `<mod_name>.pdb` | no |
| `Release` | `<project_dir>/Uploads/_staging/<mod_name>` | `<mod_name>.dll` | yes — `<mod_name>_<version>-RC.zip` written to `<project_dir>/Uploads/` |
| other | (Debug behaviour, with a warning) | (Debug behaviour) | no |

### 5.2 File copy rules

1. **Assemblies** (`<mod_name>.dll`, and `<mod_name>.pdb` for Debug) and
   `additional_output_files` are copied from `output_dir` to the deployment
   directory **always overwritten**, regardless of modification time or other
   flags.
2. **ModPackage contents** are copied from `<project_dir>/ModPackage/` to the
   deployment directory recursively:
   - Default: only files whose source mtime is newer than the target mtime.
   - Files matching any `always_copy_masks` pattern: always copied.
   - `--force` / `force: true`: all files copied regardless of mtime.
3. **`additional_files`** are copied from arbitrary locations to the
   deployment directory **always overwritten**.
4. **`--clean` / `clean: true`**: the deployment directory is removed (best
   effort) before copying begins.

### 5.3 Release packaging

For `Release` builds the script:

1. Reads the assembly version from the deployed `.dll` via .NET reflection
   (tries `pythonnet`, then PowerShell, then a compiled helper exe).
2. Writes `<mod_name>_<version>-RC.zip` into `<project_dir>/Uploads/`. The ZIP
   preserves the directory layout rooted at `<mod_name>/`.

## 6. Exit Codes

| Code | Meaning |
|---|---|
| `0` | Deployment completed without errors. |
| `1` | Deployment finished with errors (failed copies, missing files, ZIP failure) or the script raised an exception. |

Unless `--no-pause` is set, the script waits for a keypress before exiting
when the exit code is non-zero.

## 7. Statistics

On success, the script prints a summary including:

- Assemblies copied / failed.
- Files copied (newer), force-copied, always-copied (mask), skipped
  (up-to-date), failed.
- Directories created / existing.
- ZIP operation result (Release only): files in ZIP, compressed size,
  compression ratio, location.

## 8. Example `gzdeploy.yaml`

```yaml
mod_name: MyMod
build_target: Debug
output_dir: bin/{build_target}
project_dir: ./

clean: false
force: false
no_pause: false
verbose: false

always_copy_masks:
  - "*.png"

additional_output_files: []
additional_files: []
```

## 9. Example invocations

```sh
# Use defaults from gzdeploy.yaml in CWD
python deploy_target_build.py

# Pick a build target on the command line
python deploy_target_build.py Release

# Use a project-specific config
python deploy_target_build.py Debug --config "$(ProjectDir)gzdeploy.yaml"

# Wipe and fully redeploy, no pause on errors
python deploy_target_build.py Debug --config "$(ProjectDir)gzdeploy.yaml" --clean --force --no-pause
```

## 10. MSBuild integration

The script is designed to be invoked as a post-build step. A typical MSBuild
target would be:

```xml
<Exec Command="python &quot;$(ProjectDir)tools\deploy_target_build.py&quot; --config &quot;$(ProjectDir)gzdeploy.yaml&quot; $(Configuration)" />
```

Because all required values (except `BuildTarget`) come from YAML, the same
MSBuild invocation works for every mod that ships its own `gzdeploy.yaml`.

## 11. MSBuild excerpt from real project

```xml
  <PropertyGroup>
    <DeployScriptScriptFullDir>$(MSBuildProjectDirectory)\..\..\GAZ Mod Deploy</DeployScriptScriptFullDir>
  </PropertyGroup>
  <Target Name="DeployMod" AfterTargets="Build">
    <Message Text="Executing mod deployment task" Importance="High" />
    <Exec Command="python &quot;$(DeployScriptScriptFullDir)\deploy_target_build.py&quot; --config &quot;$(ProjectDir)\gzdeploy.yaml&quot; &quot;$(Configuration)&quot;" ContinueOnError="true" WorkingDirectory="$(ProjectDir)">
      <Output TaskParameter="ExitCode" PropertyName="DeployModExitCode" />
    </Exec>
    <Error Condition="'$(DeployModExitCode)' != '0'" Text="Deployment failed with exit code $(DeployModExitCode)" />
  </Target>
```

## 12. Dependencies

- Python 3.10+ (uses structural pattern matching-friendly idioms; should run on
  3.7+ in practice).
- `PyYAML` — optional but recommended. If missing, configuration loading is
  skipped (with a warning) and the script falls back to CLI-only configuration,
  which will fail validation for `mod_name`, `output_dir`, and `project_dir`.
- Windows — the script uses `%APPDATA%` for the Debug deployment directory and
  uses Windows-specific PowerShell paths for assembly version reflection.
- On non-Windows platforms the Debug deployment path falls back to standard
  XDG locations only if `APPDATA` happens to be set; the assembly version
  reflection requires PowerShell or `dotnet`.