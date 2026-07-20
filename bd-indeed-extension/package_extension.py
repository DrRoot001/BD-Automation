#!/usr/bin/env python3
import os
import sys
import zipfile
import argparse

def main():
    parser = argparse.ArgumentParser(
        description="Package the BD Indeed Auto-Apply Extension for distribution."
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Include your local config.js (containing local keys/settings) instead of the clean template config.example.js."
    )
    args = parser.parse_args()

    # Get the directory of this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # ZIP file location
    zip_filename = "bd-indeed-extension.zip"
    zip_filepath = os.path.join(script_dir, zip_filename)
    
    # Remove existing zip if it exists
    if os.path.exists(zip_filepath):
        try:
            os.remove(zip_filepath)
            print(f"[-] Removed existing {zip_filename}")
        except Exception as e:
            print(f"[!] Error removing existing zip file: {e}")
            sys.exit(1)
        
    print(f"[*] Packaging extension at: {zip_filepath}")
    
    exclude_files = {
        zip_filename,
        "package_extension.py",
        ".DS_Store",
        "config.js",  # Handled separately depending on --local flag
        "config.example.js"  # Handled separately to avoid double inclusion or naming conflict
    }
    
    exclude_dirs = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        "venv",
        ".venv",
        "node_modules"
    }
    
    count = 0
    with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # 1. Walk and add general extension files
        for root, dirs, files in os.walk(script_dir):
            # Modify dirs in-place to avoid traversing excluded dirs
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            
            for file in files:
                if file in exclude_files:
                    continue
                if file.endswith('.zip') or file.endswith('.crx'):
                    continue
                    
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, script_dir)
                
                zip_file.write(full_path, rel_path)
                print(f"[+] Added: {rel_path}")
                count += 1
                
        # 2. Add config.js based on specified options
        config_js_path = os.path.join(script_dir, "config.js")
        config_example_path = os.path.join(script_dir, "config.example.js")
        
        # Add config.example.js to the ZIP as is (always useful to have as reference)
        if os.path.exists(config_example_path):
            zip_file.write(config_example_path, "config.example.js")
            print("[+] Added: config.example.js")
            count += 1
            
        if args.local:
            if os.path.exists(config_js_path):
                zip_file.write(config_js_path, "config.js")
                print("[+] Added: config.js (using your local config.js with current settings)")
                count += 1
            else:
                print("[!] Error: --local specified but local config.js does not exist!")
                sys.exit(1)
        else:
            if os.path.exists(config_example_path):
                zip_file.write(config_example_path, "config.js")
                print("[+] Added: config.js (clean template config copied from config.example.js)")
                count += 1
            else:
                print("[!] Warning: config.example.js not found! No config.js added to zip.")

    print(f"[#] Successfully packaged {count} files into {zip_filename}")

if __name__ == "__main__":
    main()
