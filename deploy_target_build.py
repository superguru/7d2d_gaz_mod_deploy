#!/usr/bin/env python3
"""
Deploy Target Build Script

This script takes one command-line argument:
1. BuildTarget - The build configuration target (e.g., Debug, Release)

All other configuration (mod_name, output_dir, project_dir,
additional_output_files, etc.) must be supplied via gzdeploy.yaml.

The script:
1. Always copies <ModName>.dll from OutputDir to deployment directory (overwrites)
2. For Debug builds, also copies <ModName>.pdb from OutputDir (overwrites)
3. Recursively copies files from <ProjectDir>/ModPackage to deployment directories
4. For Release builds, creates a ZIP file: <ModName>_<version>-RC.zip in Uploads directory

Deployment directories based on BuildTarget:
- Debug: %APPDATA%/7DaysToDie/Mods/Dev_<ModName>
- Release: <ProjectDir>/Uploads/_staging/<ModName>

By default, only files that are newer in the source than in the target will be copied.
Files matching ALWAYS_COPY_MASKS patterns are always copied regardless of modification time.

Usage:
    python deploy_target_build.py <BuildTarget> [--config <yaml>]

Examples: (keep the dir names generic)
    python deploy_target_build.py Debug --config gzdeploy.yaml
    python deploy_target_build.py Release --config gzdeploy.yaml
    python deploy_target_build.py Debug --config gzdeploy.yaml --clean --force
"""

import sys
import os
import argparse
import shutil
import subprocess
import zipfile
import fnmatch
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, cast

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# Configuration - MOD_NAME is set from gzdeploy.yaml in main()
MOD_NAME = ""  # Set at runtime from yaml_config["mod_name"]

# File masks that should always be copied regardless of modification time
ALWAYS_COPY_MASKS = ["*.png"]

DEFAULT_CONFIG_FILE = "gzdeploy.yaml"


def load_yaml_config(config_path):
    """
    Load deployment configuration from a YAML file.

    Returns a dict with any subset of: mod_name, build_target, output_dir,
    project_dir, clean, force, no_pause, verbose, always_copy_masks.
    Returns an empty dict if the file is missing, YAML is unavailable, or
    parsing fails.
    """
    if not YAML_AVAILABLE:
        if Path(config_path).exists():
            print(
                f"Warning: PyYAML not installed; cannot load '{config_path}'. "
                "Install with: pip install pyyaml"
            )
        return {}

    path = Path(config_path)
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        print(f"Config loaded from: {config_path}")
        return config
    except Exception as e:
        print(f"Warning: Failed to load config '{config_path}': {e}")
        return {}


def substitute_yaml_value(value, substitutions):
    """
    Replace {key} placeholders in a string with values from substitutions.

    Unknown placeholders (keys not present in substitutions, or whose value
    is None) are left unchanged.  Non-string values are returned as-is.
    """
    if not isinstance(value, str):
        return value
    result = value
    for key, sub_value in substitutions.items():
        if sub_value is not None:
            result = result.replace("{" + key + "}", str(sub_value))
    return result


def substitute_yaml_values(config, substitutions):
    """
    Recursively apply {key} substitution to all string values in a YAML
    structure (dicts, lists, and scalars).
    """
    if isinstance(config, dict):
        return {k: substitute_yaml_values(v, substitutions) for k, v in config.items()}
    if isinstance(config, list):
        return [substitute_yaml_values(item, substitutions) for item in config]
    return substitute_yaml_value(config, substitutions)


def resolve_core_values(yaml_config, cli_build_target=None):
    """
    Resolve the four substitutable core values (mod_name, build_target,
    output_dir, project_dir) by iteratively substituting {key} placeholders.

    CLI build_target (when provided) overrides the YAML build_target as
    the starting value.  Iterative resolution handles cross-references such
    as ``project_dir: "{mod_name}_src"`` with ``mod_name: MyMod``.
    """
    substitutions = {
        "mod_name": yaml_config.get("mod_name"),
        "build_target": cli_build_target
        if cli_build_target is not None
        else yaml_config.get("build_target"),
        "output_dir": yaml_config.get("output_dir"),
        "project_dir": yaml_config.get("project_dir"),
    }

    for _ in range(10):
        changed = False
        for key in list(substitutions.keys()):
            new_value = substitute_yaml_value(substitutions[key], substitutions)
            if new_value != substitutions[key]:
                substitutions[key] = new_value
                changed = True
        if not changed:
            break

    return substitutions


def get_assembly_version_via_reflection(dll_path):
    """
    Get assembly version using .NET reflection via PowerShell or direct .NET call.

    Args:
        dll_path (str): Path to the DLL file

    Returns:
        str: Version string (e.g., "2.4.0.0") or None if extraction fails
    """
    try:
        # Try using pythonnet if available
        try:
            import sys

            sys.path.append(os.path.dirname(dll_path))

            from System.Reflection import Assembly  # type: ignore[import]

            assembly = Assembly.LoadFrom(os.path.abspath(dll_path))
            version = assembly.GetName().Version
            return f"{version.Major}.{version.Minor}.{version.Build}.{version.Revision}"

        except ImportError:
            # Fallback to PowerShell approach
            pass

        # Use PowerShell to get assembly version.
        # Try multiple locations since MSBuild may not have powershell in PATH.
        powershell_script = f"""
        try {{
            $assembly = [System.Reflection.Assembly]::LoadFrom('{dll_path.replace("'", "''")}')
            $version = $assembly.GetName().Version
            Write-Output "$($version.Major).$($version.Minor).$($version.Build).$($version.Revision)"
        }} catch {{
            Write-Output "Error: $($_.Exception.Message)"
        }}
        """

        _ps_candidates = [
            "powershell",
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe",
            "pwsh",
        ]
        result = None
        for _ps in _ps_candidates:
            try:
                result = subprocess.run(
                    [_ps, "-Command", powershell_script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                break
            except (FileNotFoundError, OSError):
                continue
        if result is None:
            return None

        if result.returncode == 0:
            version = result.stdout.strip()
            if version and not version.startswith("Error:"):
                return version

        return None

    except Exception as e:
        print(f"Warning: Failed to get assembly version via reflection: {e}")
        return None


def get_assembly_version_via_dotnet_tool(dll_path):
    """
    Get assembly version using dotnet CLI or custom tool.

    Args:
        dll_path (str): Path to the DLL file

    Returns:
        str: Version string or None if extraction fails
    """
    try:
        # Try using dotnet CLI if available
        result = subprocess.run(
            ["dotnet", "--version"], capture_output=True, text=True, timeout=5
        )

        if result.returncode == 0:
            # Use a simple C# program to get the version
            temp_cs_file = os.path.join(os.path.dirname(dll_path), "GetVersion.cs")
            exe_file = temp_cs_file.replace(".cs", ".exe")
            cs_code = f'''
using System;
using System.Reflection;

class Program
{{
    static void Main()
    {{
        try
        {{
            var assembly = Assembly.LoadFrom(@"{dll_path}");
            var version = assembly.GetName().Version;
            Console.WriteLine($"{{version.Major}}.{{version.Minor}}.{{version.Build}}.{{version.Revision}}");
        }}
        catch (Exception ex)
        {{
            Console.WriteLine($"Error: {{ex.Message}}");
        }}
    }}
}}
'''
            try:
                # Write temporary C# file
                with open(temp_cs_file, "w") as f:
                    f.write(cs_code)

                # Compile and run
                compile_result = subprocess.run(
                    ["csc", "/out:" + exe_file, temp_cs_file],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                if compile_result.returncode == 0:
                    run_result = subprocess.run(
                        [exe_file], capture_output=True, text=True, timeout=5
                    )

                    if run_result.returncode == 0:
                        version = run_result.stdout.strip()
                        if version and not version.startswith("Error:"):
                            return version
                    else:
                        print(
                            f"Warning: Failed to run version extractor: {run_result.stderr}"
                        )
                else:
                    print(
                        f"Warning: Failed to compile version extractor: {compile_result.stderr}"
                    )
            finally:
                # Always clean up temp files
                for _f in (temp_cs_file, exe_file):
                    try:
                        if os.path.exists(_f):
                            os.remove(_f)
                    except OSError:
                        pass

        return None

    except Exception as e:
        print(f"Warning: Failed to get assembly version via dotnet tool: {e}")
        return None


def get_assembly_version(dll_path):
    """
    Get assembly version using various .NET reflection methods.

    Args:
        dll_path (str): Path to the DLL file

    Returns:
        str: Version string or "Unknown" if all methods fail
    """
    if not os.path.exists(dll_path):
        return "File not found"

    # Try reflection approach (pythonnet / PowerShell)
    version = get_assembly_version_via_reflection(dll_path)
    if version:
        return version

    # Try dotnet tool approach
    version = get_assembly_version_via_dotnet_tool(dll_path)
    if version:
        return version

    return "Unknown"


def create_release_zip(deploy_dir, uploads_dir, assembly_version, stats):
    """
    Create a ZIP file of the mod directory for Release builds.

    Args:
        deploy_dir (str): Path to the deployed mod directory
        uploads_dir (str): Path to the Uploads directory (parent of _staging)
        assembly_version (str): Assembly version string
        stats (DeploymentStats): Statistics tracker

    Returns:
        str: Path to created ZIP file or None if failed
    """
    try:
        # Ensure uploads directory exists
        os.makedirs(uploads_dir, exist_ok=True)

        # Create ZIP filename with full version
        if assembly_version and assembly_version not in ["Unknown", "File not found"]:
            # Use the full version as provided (e.g., "2.4.0.0")
            version_string = assembly_version
        else:
            version_string = "0.0.0.0"

        zip_filename = f"{MOD_NAME}_{version_string}-RC.zip"
        zip_path = os.path.join(uploads_dir, zip_filename)

        # Remove existing ZIP file if it exists
        if os.path.exists(zip_path):
            os.remove(zip_path)
            print(f"Removed existing ZIP file: {zip_filename}")

        # Create ZIP file
        print(f"Creating release ZIP: {zip_filename}")

        with zipfile.ZipFile(
            zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zipf:
            # Get the mod directory name (should be MOD_NAME)
            mod_dir_name = os.path.basename(deploy_dir)

            # Walk through all files in the deploy directory
            files_added = 0
            total_size = 0

            for root, dirs, files in os.walk(deploy_dir):
                for file in files:
                    file_path = os.path.join(root, file)

                    # Calculate relative path from deploy_dir
                    rel_path = os.path.relpath(file_path, deploy_dir)

                    # Add to ZIP with mod directory structure
                    arcname = os.path.join(mod_dir_name, rel_path).replace(
                        os.path.sep, "/"
                    )

                    zipf.write(file_path, arcname)
                    files_added += 1
                    total_size += os.path.getsize(file_path)

                    print(f"  Added to ZIP: {rel_path}")

        # Get ZIP file size
        zip_size = os.path.getsize(zip_path)
        compression_ratio = (1 - zip_size / total_size) * 100 if total_size > 0 else 0

        print("ZIP created successfully:")
        print(f"  Files: {files_added}")
        print(f"  Original size: {total_size / 1024:.1f} KB")
        print(f"  Compressed size: {zip_size / 1024:.1f} KB")
        print(f"  Compression: {compression_ratio:.1f}%")
        print(f"  Location: {zip_path}")

        # Update statistics
        stats.zip_created = True
        stats.zip_path = zip_path
        stats.zip_files_count = files_added
        stats.zip_size = zip_size

        return zip_path

    except Exception as e:
        print(f"Failed to create release ZIP: {e}", file=sys.stderr)
        stats.zip_failed = True
        stats.zip_error = str(e)
        return None


class DeploymentStats:
    """Class to track deployment statistics"""

    def __init__(self):
        self.files_copied = 0
        self.files_skipped = 0
        self.files_up_to_date = 0
        self.files_failed = 0
        self.files_forced = 0
        self.files_always_copy = 0
        self.assemblies_copied = 0
        self.assemblies_failed = 0
        self.directories_created = 0
        self.directories_existing = 0
        self.total_files_processed = 0
        self.total_bytes_copied = 0
        self.zip_created = False
        self.zip_failed = False
        self.zip_path = None
        self.zip_files_count = 0
        self.zip_size = 0
        self.zip_error = None
        self.start_time = datetime.now()
        self.copied_files = []
        self.skipped_files = []
        self.failed_files = []
        self.forced_files = []
        self.always_copy_files = []
        self.assembly_files = []

    def file_copied(self, source_file, target_file, file_size):
        """Record a successful file copy"""
        self.files_copied += 1
        self.total_bytes_copied += file_size
        self.copied_files.append((source_file, target_file))

    def file_forced(self, source_file, target_file, file_size):
        """Record a file that was force-copied"""
        self.files_forced += 1
        self.total_bytes_copied += file_size
        self.forced_files.append((source_file, target_file))

    def file_always_copy(self, source_file, target_file, file_size):
        """Record a file that was copied due to always-copy mask"""
        self.files_always_copy += 1
        self.total_bytes_copied += file_size
        self.always_copy_files.append((source_file, target_file))

    def assembly_copied(self, source_file, target_file, file_size):
        """Record a successful assembly file copy"""
        self.assemblies_copied += 1
        self.total_bytes_copied += file_size
        self.assembly_files.append((source_file, target_file))

    def assembly_failed(self, source_file, error):
        """Record a failed assembly file copy"""
        self.assemblies_failed += 1
        self.failed_files.append((source_file, str(error)))

    def file_skipped_up_to_date(self, source_file):
        """Record a file that was skipped because it's up to date"""
        self.files_up_to_date += 1
        self.skipped_files.append(source_file)

    def file_failed(self, source_file, error):
        """Record a file that failed to copy"""
        self.files_failed += 1
        self.failed_files.append((source_file, str(error)))

    def directory_created(self):
        """Record a directory creation"""
        self.directories_created += 1

    def directory_existing(self):
        """Record an existing directory"""
        self.directories_existing += 1

    def get_duration(self):
        """Get the elapsed time since start"""
        return datetime.now() - self.start_time

    def has_errors(self):
        """Check if there were any errors during deployment"""
        return self.files_failed > 0 or self.assemblies_failed > 0 or self.zip_failed

    def print_summary(self):
        """Print comprehensive deployment statistics"""
        duration = self.get_duration()

        print("=" * 60)
        print("DEPLOYMENT STATISTICS")
        print("=" * 60)
        print(f"Total execution time: {duration.total_seconds():.2f} seconds")
        print()

        print("Assembly Operations:")
        print(f"  [DLL] Assemblies copied:         {self.assemblies_copied}")
        print(f"  [ER]  Assembly copy failed:      {self.assemblies_failed}")
        print()

        print("File Operations:")
        print(f"  [OK] Files copied (newer):       {self.files_copied}")
        print(f"  [>>] Files force-copied:         {self.files_forced}")
        print(f"  [**] Files always-copied (mask): {self.files_always_copy}")
        print(f"  [==] Files skipped (up-to-date): {self.files_up_to_date}")
        print(f"  [ER] Files failed:               {self.files_failed}")
        print(
            f"  [##] Total files processed:      {self.files_copied + self.files_forced + self.files_always_copy + self.files_up_to_date + self.files_failed}"
        )
        print()

        print("Directory Operations:")
        print(f"  [+] Directories created:         {self.directories_created}")
        print(f"  [=] Directories existing:        {self.directories_existing}")
        print()

        # ZIP Operations (for Release builds)
        if self.zip_created or self.zip_failed:
            print("ZIP Operations:")
            if self.zip_created:
                print("  [ZIP] Release ZIP created:     1")
                print(f"  [ZIP] Files in ZIP:            {self.zip_files_count}")
                print(f"  [ZIP] ZIP size:                {self.zip_size / 1024:.1f} KB")
                print(
                    f"  [ZIP] ZIP location:            {os.path.basename(self.zip_path) if self.zip_path else 'N/A'}"
                )
            if self.zip_failed:
                print("  [ER]  ZIP creation failed:     1")
                if self.zip_error:
                    print(f"  [ER]  ZIP error:               {self.zip_error}")
            print()

        if self.total_bytes_copied > 0:
            if self.total_bytes_copied > 1024 * 1024:
                size_str = f"{self.total_bytes_copied / (1024 * 1024):.2f} MB"
            elif self.total_bytes_copied > 1024:
                size_str = f"{self.total_bytes_copied / 1024:.2f} KB"
            else:
                size_str = f"{self.total_bytes_copied} bytes"
            print(f"Total data copied: {size_str}")
            print()

        # Show assembly files if any
        if self.assembly_files:
            print("Assembly files copied:")
            for source, target in self.assembly_files:
                print(f"  [DLL] {os.path.basename(source)}")
            print()

        # Show copied files if any
        all_copied = self.copied_files + self.forced_files + self.always_copy_files
        if all_copied and len(all_copied) <= 20:
            print("Package files copied:")
            for source, target in self.copied_files:
                print(f"  [OK] {os.path.basename(source)}")
            for source, target in self.forced_files:
                print(f"  [>>] {os.path.basename(source)} (forced)")
            for source, target in self.always_copy_files:
                print(f"  [**] {os.path.basename(source)} (always-copy mask)")
            print()
        elif len(all_copied) > 20:
            print(f"Package files copied: {len(all_copied)} files (too many to list)")
            if self.files_forced > 0:
                print(f"  ({self.files_forced} were force-copied)")
            if self.files_always_copy > 0:
                print(f"  ({self.files_always_copy} were always-copied due to mask)")
            print()

        # Show failed files if any
        if self.failed_files:
            print("Failed files:")
            for source, error in self.failed_files:
                rel_source = os.path.basename(source)
                print(f"  [ER] {rel_source}: {error}")
            print()

        # Overall status
        if self.has_errors():
            print("WARNING: Deployment completed with errors!")
        elif (
            self.files_copied
            + self.files_forced
            + self.files_always_copy
            + self.assemblies_copied
        ) > 0 or self.zip_created:
            print("SUCCESS: Deployment completed successfully!")
        elif self.files_up_to_date > 0:
            print("INFO: All files are up to date - no copying needed.")
        else:
            print("WARNING: No files found to process.")


def get_deploy_directory(build_target, project_dir):
    """
    Get the deployment directory path based on the build target.

    Args:
        build_target (str): Build configuration target (Debug, Release, etc.)
        project_dir (str): Project directory path

    Returns:
        str: Full path to the deployment directory
    """
    build_target_lower = build_target.lower()

    if build_target_lower == "debug":
        # Debug builds go to APPDATA with Dev_ prefix
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise EnvironmentError("APPDATA environment variable not found")

        deploy_dir = os.path.join(appdata, "7DaysToDie", "Mods", f"Dev_{MOD_NAME}")
        return deploy_dir

    elif build_target_lower == "release":
        # Release builds go to project Uploads/_staging without Dev_ prefix
        deploy_dir = os.path.join(project_dir, "Uploads", "_staging", MOD_NAME)
        return deploy_dir

    else:
        # For other build targets, default to Debug behavior but show warning
        print(
            f"Warning: Unknown build target '{build_target}', defaulting to Debug behavior"
        )
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise EnvironmentError("APPDATA environment variable not found")

        deploy_dir = os.path.join(appdata, "7DaysToDie", "Mods", f"Dev_{MOD_NAME}")
        return deploy_dir


def ensure_directory_exists(directory_path, stats):
    """
    Ensure that the specified directory exists, creating it if necessary.

    Args:
        directory_path (str): Path to the directory
        stats (DeploymentStats): Statistics tracker
    """
    try:
        if not os.path.exists(directory_path):
            os.makedirs(directory_path, exist_ok=True)
            stats.directory_created()
            print(f"Created directory: {directory_path}")
        else:
            stats.directory_existing()
    except OSError as e:
        raise OSError(f"Failed to create directory {directory_path}: {e}")


def copy_assembly_files(
    output_dir, deploy_dir, build_target, stats, additional_files=None
):
    """
    Copy assembly files (.dll and .pdb for Debug) from output directory to deployment directory.
    Optionally copies extra files/patterns from output_dir specified via additional_files.
    All files handled here are always overwritten regardless of modification time or other flags.

    Args:
        output_dir (str): Output directory containing built assemblies
        deploy_dir (str): Deployment directory
        build_target (str): Build target (Debug, Release, etc.)
        stats (DeploymentStats): Statistics tracker
        additional_files (list[str] | None): Extra filenames or glob patterns to copy from output_dir
    """
    assembly_files = [f"{MOD_NAME}.dll"]

    # Add .pdb file for Debug builds
    if build_target.lower() == "debug":
        assembly_files.append(f"{MOD_NAME}.pdb")

    print("Copying assembly files (always overwrite):")

    for assembly_file in assembly_files:
        source_file = os.path.join(output_dir, assembly_file)
        target_file = os.path.join(deploy_dir, assembly_file)

        try:
            if not os.path.exists(source_file):
                print(f"Warning: Assembly file not found: {source_file}")
                stats.assembly_failed(source_file, "File not found")
                continue

            file_size = os.path.getsize(source_file)
            shutil.copy2(source_file, target_file)
            stats.assembly_copied(source_file, target_file, file_size)
            print(f"Assembly copied: {assembly_file}")

        except (IOError, OSError) as e:
            stats.assembly_failed(source_file, e)
            print(f"Failed to copy assembly {assembly_file}: {e}", file=sys.stderr)

    # Copy additional output files (always overwrite, same rules as assemblies)
    if additional_files:
        output_path = Path(output_dir)
        for pattern in additional_files:
            matches = sorted(output_path.glob(pattern))
            if not matches:
                print(f"Warning: No files matched '{pattern}' in {output_dir}")
                stats.assembly_failed(
                    os.path.join(output_dir, pattern), "No files matched pattern"
                )
                continue
            for match in matches:
                source_file = str(match)
                target_file = os.path.join(deploy_dir, match.name)
                try:
                    file_size = os.path.getsize(source_file)
                    shutil.copy2(source_file, target_file)
                    stats.assembly_copied(source_file, target_file, file_size)
                    print(f"Additional file copied: {match.name}")
                except (IOError, OSError) as e:
                    stats.assembly_failed(source_file, e)
                    print(f"Failed to copy {match.name}: {e}", file=sys.stderr)

    print()


def copy_additional_files(file_specs, deploy_dir, project_dir, stats):
    """
    Copy arbitrary files to the deployment directory.

    Each entry in file_specs is a path (absolute, or relative to project_dir)
    and may contain glob wildcards in the filename part (e.g. *.config).
    All matched files are always overwritten, same as DLL/PDB.

    Args:
        file_specs (list[str]): File paths or glob patterns to copy
        deploy_dir (str): Deployment directory
        project_dir (str): Used to resolve relative paths
        stats (DeploymentStats): Statistics tracker
    """
    if not file_specs:
        return

    project_path = Path(project_dir)
    print("Copying additional files (always overwrite):")

    for spec in file_specs:
        spec_path = Path(spec)
        if not spec_path.is_absolute():
            spec_path = project_path / spec_path

        matches = sorted(spec_path.parent.glob(spec_path.name))
        if not matches:
            print(f"Warning: No files matched '{spec}'")
            stats.assembly_failed(str(spec_path), "No files matched")
            continue

        for match in matches:
            source_file = str(match)
            target_file = os.path.join(deploy_dir, match.name)
            try:
                file_size = os.path.getsize(source_file)
                shutil.copy2(source_file, target_file)
                stats.assembly_copied(source_file, target_file, file_size)
                print(f"Additional file copied: {match.name}  (from {match.parent})")
            except (IOError, OSError) as e:
                stats.assembly_failed(source_file, e)
                print(f"Failed to copy {match.name}: {e}", file=sys.stderr)

    print()


def should_copy_file(source_file, target_file, force_copy=False):
    """
    Determine if a file should be copied based on modification time, force flag, or always-copy masks.

    Args:
        source_file (str): Path to source file
        target_file (str): Path to target file
        force_copy (bool): If True, always copy regardless of modification time

    Returns:
        tuple: (should_copy, copy_reason) where should_copy is bool and copy_reason is 'forced', 'always', 'newer', or None
    """
    # Check if file matches any always-copy mask
    filename = os.path.basename(source_file)
    for mask in ALWAYS_COPY_MASKS:
        if fnmatch.fnmatch(filename, mask):
            return True, "always"

    # If force_copy is True, always copy
    if force_copy:
        return True, "forced"

    # If target doesn't exist, always copy
    if not os.path.exists(target_file):
        return True, "newer"

    try:
        source_mtime = os.path.getmtime(source_file)
        target_mtime = os.path.getmtime(target_file)

        # Copy if source is newer than target
        if source_mtime > target_mtime:
            return True, "newer"
        return False, None
    except OSError:
        # If we can't get modification times, err on the side of copying
        return True, "newer"


def copy_mod_package(source_dir, target_dir, stats, force_copy=False):
    """
    Recursively copy files and directories from source to target.
    Only copies files that are newer in source than in target unless force_copy is True
    or the file matches an always-copy mask.

    Args:
        source_dir (str): Source directory path (ModPackage)
        target_dir (str): Target directory path (deployment directory)
        stats (DeploymentStats): Statistics tracker
        force_copy (bool): If True, copy all files regardless of modification time
    """
    # Walk through all files and directories in source
    for root, dirs, files in os.walk(source_dir):
        # Calculate relative path from source_dir
        rel_path = os.path.relpath(root, source_dir)

        # Create corresponding directory in target
        if rel_path == ".":
            target_root = target_dir
        else:
            target_root = os.path.join(target_dir, rel_path)

        # Ensure the directory exists
        ensure_directory_exists(target_root, stats)

        # Process all files in current directory
        for file in files:
            source_file = os.path.join(root, file)
            target_file = os.path.join(target_root, file)

            try:
                # Check if file should be copied
                should_copy, copy_reason = should_copy_file(
                    source_file, target_file, force_copy
                )

                if should_copy:
                    # Get file size before copying
                    file_size = os.path.getsize(source_file)

                    # Copy the file
                    shutil.copy2(source_file, target_file)

                    rel_source = os.path.relpath(source_file, source_dir)

                    if copy_reason == "always":
                        stats.file_always_copy(source_file, target_file, file_size)
                        print(f"Always-copied (mask): {rel_source}")
                    elif copy_reason == "forced":
                        stats.file_forced(source_file, target_file, file_size)
                        print(f"Force-copied: {rel_source}")
                    else:  # 'newer'
                        stats.file_copied(source_file, target_file, file_size)
                        print(f"Copied: {rel_source}")
                else:
                    # File is up to date
                    stats.file_skipped_up_to_date(source_file)
                    rel_source = os.path.relpath(source_file, source_dir)
                    print(f"Up-to-date: {rel_source}")

            except (IOError, OSError) as e:
                stats.file_failed(source_file, e)
                print(
                    f"Failed to copy {os.path.relpath(source_file, source_dir)}: {e}",
                    file=sys.stderr,
                )


def clean_deploy_directory(deploy_dir):
    """
    Clean the deployment directory by removing all existing files and directories.

    Args:
        deploy_dir (str): Deployment directory path
    """
    if os.path.exists(deploy_dir):
        print(f"Cleaning deployment directory: {deploy_dir}")
        try:
            shutil.rmtree(deploy_dir)
            print("Deployment directory cleaned successfully")
        except (IOError, OSError) as e:
            print(
                f"Warning: Failed to clean deployment directory: {e}", file=sys.stderr
            )


def wait_for_keypress():
    """
    Wait for user to press any key before continuing.
    Cross-platform implementation.
    """
    try:
        print("\nPress any key to continue...")

        # Try to use platform-specific method
        if os.name == "nt":  # Windows
            import msvcrt

            msvcrt.getch()
        else:  # Unix/Linux/macOS
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(sys.stdin.fileno())
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    except (ImportError, OSError):
        # Fallback to input() if platform-specific methods fail
        input("Press Enter to continue...")


def main():
    """Main function to parse arguments and deploy the mod."""
    global MOD_NAME, ALWAYS_COPY_MASKS

    # Pre-scan sys.argv for --config so we can load YAML before full parse.
    config_path = None
    _argv = sys.argv[1:]
    for i, arg in enumerate(_argv):
        if arg == "--config" and i + 1 < len(_argv):
            config_path = _argv[i + 1]
            break
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            break

    # When no --config is given, search: CWD first, then the script's own
    # directory.  This lets a project-level gzdeploy.yaml (in the MSBuild
    # project dir) take precedence over the shared one next to this script.
    if config_path is None:
        _cwd_config = Path(DEFAULT_CONFIG_FILE)
        _script_config = Path(__file__).parent / DEFAULT_CONFIG_FILE
        if _cwd_config.exists():
            config_path = str(_cwd_config)
        elif _script_config.exists():
            config_path = str(_script_config)
        else:
            config_path = (
                DEFAULT_CONFIG_FILE  # neither exists; load_yaml_config returns {}
            )

    yaml_config = load_yaml_config(config_path)

    # Pre-extract ModName for help text (from YAML only).
    _mod_name_preview = yaml_config.get("mod_name") or "<ModName>"
    MOD_NAME = _mod_name_preview

    # Create argument parser
    parser = argparse.ArgumentParser(
        description=f"Deploy Target Build Script - deploys {MOD_NAME} mod to target-specific directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s Debug
  %(prog)s Release
  %(prog)s Debug --config path/to/gzdeploy.yaml
  %(prog)s Debug --clean --force
  %(prog)s Debug --no-pause
  %(prog)s  (uses gzdeploy.yaml in current directory)

This script will:
1. Copy assembly files from <OutputDir> (always overwrite):
   - <ModName>.dll (for all build targets)
   - <ModName>.pdb (for Debug builds only)

2. Copy package files from <ProjectDir>/ModPackage to:
   DEBUG builds:   %%APPDATA%%/7DaysToDie/Mods/Dev_<ModName>
   RELEASE builds: <ProjectDir>/Uploads/_staging/<ModName>

3. For RELEASE builds: Create <ModName>_<version>-RC.zip in <ProjectDir>/Uploads/

By default, only package files that are newer will be copied (incremental deployment).
Assembly files are ALWAYS copied regardless of modification time or other flags.
Files matching always_copy_masks patterns (e.g., *.png) are always copied.
Use --force to copy all package files regardless of modification time.
Use --clean to remove all existing files before copying.
Use --no-pause to skip pausing on errors (default: pause on errors).

Config file (gzdeploy.yaml) keys:
  mod_name, build_target, output_dir, project_dir,
  clean, force, no_pause, verbose, always_copy_masks,
  additional_output_files, additional_files

Any string value may reference the four core values via {mod_name},
{build_target}, {output_dir}, or {project_dir}.  Cross-references
(e.g. project_dir: "{mod_name}_src") are resolved iteratively.
        """,
    )

    # BuildTarget is the only positional argument; mod_name, output_dir, and
    # project_dir must be supplied via gzdeploy.yaml.
    parser.add_argument(
        "BuildTarget",
        nargs="?",
        default=None,
        help="Build configuration target (e.g., Debug, Release)",
    )

    # Optional flags — defaults come from YAML (or False if absent).
    parser.add_argument(
        "--clean",
        action="store_true",
        default=yaml_config.get("clean", False),
        help="Clean the deployment directory before copying (default: False)",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        default=yaml_config.get("force", False),
        help="Copy all package files regardless of modification time (default: False)",
    )

    parser.add_argument(
        "--no-pause",
        action="store_true",
        default=yaml_config.get("no_pause", False),
        help="Skip pausing for keypress on errors or exceptions (default: False, meaning pause on errors)",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        default=yaml_config.get("verbose", False),
        help="Show detailed output for all file operations",
    )

    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        metavar="FILE",
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG_FILE})",
    )

    # Parse arguments
    try:
        args = parser.parse_args()
    except SystemExit:
        return 1

    # Resolve the four substitutable core values (mod_name, build_target,
    # output_dir, project_dir).  CLI build_target overrides the YAML value;
    # cross-references between core values (e.g. project_dir: "{mod_name}_src")
    # are resolved iteratively.
    core_values = resolve_core_values(yaml_config, cli_build_target=args.BuildTarget)
    args.BuildTarget = core_values["build_target"]
    args.ModName = core_values["mod_name"]
    args.OutputDir = core_values["output_dir"]
    args.ProjectDir = core_values["project_dir"]

    # Apply {key} substitution to all string values in the YAML config so
    # entries like always_copy_masks, additional_files, and
    # additional_output_files can reference the resolved core values.
    # yaml.safe_load always returns a dict for our config file; cast tells
    # the type checker to keep that assumption.
    yaml_config = cast(Dict[str, Any], substitute_yaml_values(yaml_config, core_values))

    # Validate that all required fields are now resolved.
    missing = [
        name
        for name, val in [
            ("mod_name", args.ModName),
            ("build_target", args.BuildTarget),
            ("output_dir", args.OutputDir),
            ("project_dir", args.ProjectDir),
        ]
        if not val
    ]
    if missing:
        parser.error(
            f"Missing required value(s) in gzdeploy.yaml: {', '.join(missing)}. "
            "Provide build_target as a positional argument (or in YAML) and "
            "mod_name, output_dir, and project_dir in gzdeploy.yaml."
        )

    # Apply always_copy_masks from YAML (only if YAML provided the key).
    if "always_copy_masks" in yaml_config:
        masks = yaml_config["always_copy_masks"]
        if isinstance(masks, list):
            ALWAYS_COPY_MASKS[:] = masks

    # additional_output_files comes from YAML only (no CLI option).
    yaml_extra = yaml_config.get("additional_output_files") or []
    args.additional_output_files = yaml_extra if isinstance(yaml_extra, list) else []

    # Set the global MOD_NAME from the resolved argument
    MOD_NAME = args.ModName

    # Track if we had an exception or errors for pause decision
    had_exception = False
    exit_code = 0
    assembly_version = None

    try:
        # Initialize statistics
        stats = DeploymentStats()

        # Determine deployment mode
        mode_parts = []
        if args.clean:
            mode_parts.append("Clean")
        if args.force:
            mode_parts.append("Force")

        if mode_parts:
            mode = " + ".join(mode_parts) + " deployment"
        else:
            mode = "Incremental deployment"

        # Print configuration
        print(f"Deploy Target Build Script for {MOD_NAME}")
        print("=" * 50)
        print(
            f"Command: {' '.join(f'{arg}' if i == 0 else f'"{arg}"' for i, arg in enumerate(sys.argv))}"
        )
        print(f"ModName: {args.ModName}")
        print(f"BuildTarget: {args.BuildTarget}")
        print(f"OutputDir: {args.OutputDir}")
        print(f"ProjectDir: {args.ProjectDir}")
        print(f"Mode: {mode}")
        if ALWAYS_COPY_MASKS:
            print(f"Always-copy masks: {', '.join(ALWAYS_COPY_MASKS)}")
        if args.additional_output_files:
            print(f"Additional output files: {', '.join(args.additional_output_files)}")
        additional_files = yaml_config.get("additional_files") or []
        if not isinstance(additional_files, list):
            additional_files = []
        if additional_files:
            print(f"Additional files: {', '.join(additional_files)}")
        print()

        # Validate ProjectDir
        project_path = Path(args.ProjectDir)
        if not project_path.exists():
            print(
                f"Error: Project directory does not exist: {args.ProjectDir}",
                file=sys.stderr,
            )
            return 1

        # Validate OutputDir
        output_path = Path(args.OutputDir)
        if not output_path.exists():
            print(
                f"Error: Output directory does not exist: {args.OutputDir}",
                file=sys.stderr,
            )
            return 1

        # Check for ModPackage directory
        mod_package_dir = project_path / "ModPackage"
        if not mod_package_dir.exists():
            print(
                f"Error: ModPackage directory not found: {mod_package_dir}",
                file=sys.stderr,
            )
            return 1

        # Get deployment directory based on build target
        deploy_dir = get_deploy_directory(args.BuildTarget, str(project_path))
        print(f"DeployDir: {deploy_dir}")

        # Show deployment strategy
        if args.BuildTarget.lower() == "debug":
            print(
                "Deployment strategy: Debug build -> Development directory (APPDATA with Dev_ prefix)"
            )
            print(f"Assembly files: {MOD_NAME}.dll, {MOD_NAME}.pdb")
        elif args.BuildTarget.lower() == "release":
            print(
                "Deployment strategy: Release build -> Staging directory (ProjectDir/Uploads/_staging)"
            )
            print(f"Assembly files: {MOD_NAME}.dll")
        else:
            print(
                f"Deployment strategy: Unknown target '{args.BuildTarget}' -> Defaulting to Debug behavior"
            )
            print(f"Assembly files: {MOD_NAME}.dll, {MOD_NAME}.pdb")
        print()

        # Clean deployment directory if requested
        if args.clean:
            clean_deploy_directory(deploy_dir)

        # Ensure deployment directory exists
        ensure_directory_exists(deploy_dir, stats)

        # Copy assembly files first (always overwrite)
        copy_assembly_files(
            str(output_path),
            deploy_dir,
            args.BuildTarget,
            stats,
            args.additional_output_files,
        )

        # Copy arbitrary additional files from any location (YAML-only, always overwrite)
        copy_additional_files(additional_files, deploy_dir, str(project_path), stats)

        # Copy ModPackage contents to deployment directory
        print("Copying package files:")
        print(f"From: {mod_package_dir}")
        print(f"To: {deploy_dir}")
        print()

        copy_mod_package(str(mod_package_dir), deploy_dir, stats, args.force)

        # For Release builds, get assembly version from the deployed DLL and create ZIP file
        if args.BuildTarget.lower() == "release":
            print()
            dll_path = os.path.join(deploy_dir, f"{MOD_NAME}.dll")
            assembly_version = get_assembly_version(dll_path)
            print(f"Assembly Version (from deploy dir): {assembly_version}")
            uploads_dir = os.path.join(str(project_path), "Uploads")
            create_release_zip(deploy_dir, uploads_dir, assembly_version, stats)

        # Print comprehensive statistics
        stats.print_summary()

        # Set exit code based on results
        exit_code = 1 if stats.has_errors() else 0

    except Exception as e:
        had_exception = True
        exit_code = 1
        print(f"\nError during deployment: {e}", file=sys.stderr)

        # Try to print partial statistics if available
        try:
            if "stats" in locals():
                print("\nPartial statistics before error:")
                stats.print_summary()
        except Exception:
            pass  # Don't let statistics printing cause additional errors

    finally:
        # Pause if there were exceptions or failed files, unless --no-pause is specified
        should_pause = not args.no_pause and (
            had_exception or ("stats" in locals() and stats.has_errors())
        )

        if should_pause:
            print("\n" + "=" * 60)
            if had_exception:
                print("WARNING: Script encountered an exception!")
            if "stats" in locals() and stats.has_errors():
                total_errors = stats.files_failed + stats.assemblies_failed
                if stats.zip_failed:
                    total_errors += 1
                print(f"WARNING: {total_errors} operation(s) failed!")

            print("Please review the error messages above.")
            wait_for_keypress()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
