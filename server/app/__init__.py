#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Selfserve Portal for the Apache Software Foundation"""
import re
import secrets
import quart
from .lib import config, log, middleware
import os
import hashlib
import base64

STATIC_DIR = os.path.join(os.path.realpath(".."), "htdocs")  # File location of static assets
TEMPLATES_DIR = os.path.join(STATIC_DIR, "templates")  # HTML master templates
COMPILED_DIR = os.path.join(STATIC_DIR, "compiled")    # Compiled HTML (template + content)


def file_to_sri(filepath: str):
    """Generates a sub-resource integrity value for a file - https://www.w3.org/TR/SRI/"""
    with open(filepath, "rb") as f:
        digest = hashlib.sha384(f.read()).digest()
        b64_digest = base64.b64encode(digest).decode('us-ascii')
        return f"sha384-{b64_digest}"


def main():
    app = quart.Quart(__name__)
    app.secret_key = secrets.token_hex()  # For session management
    app.config["MAX_CONTENT_LENGTH"] = config.server.max_content_length  # Ensure upload limits match expectations
    app.url_map.converters["filename"] = middleware.FilenameConverter  # Special converter for filename-style vars

    # Static files (or index.html if requesting a dir listing)
    @app.route("/<path:path>")
    @app.route("/")
    async def static_files(path="index.html"):
        if path.endswith("/"):
            path += "index.html"
        if path.endswith(".html"):  # Serve HTML from the compiled output dir
            return await quart.send_from_directory(COMPILED_DIR, path)
        return await quart.send_from_directory(STATIC_DIR, path)

    @app.before_serving
    async def compile_html():
        """Compiles HTML files in htdocs/ using a master template"""
        master_template = open(os.path.join(TEMPLATES_DIR, "master.html")).read()
        # Add sub-resource integrity to all scripts
        for script_src in re.finditer(r'(src="(.+?\.js)")', master_template):
            script_name = script_src.group(2).lstrip("/")
            script_path = os.path.join(STATIC_DIR, script_name)
            if os.path.isfile(script_path):
                sri = file_to_sri(script_path)
                orig_src = script_src.group(1)
                new_src = f"{orig_src} integrity=\"{sri}\""
                master_template = master_template.replace(orig_src, new_src)
        if not os.path.isdir(COMPILED_DIR):
            log.log(f"Compiled HTML directory {COMPILED_DIR} does not exist, will attempt to create it")
            os.makedirs(COMPILED_DIR, exist_ok=True, mode=0o700)
        for htmlfile in [filename for filename in os.listdir(STATIC_DIR) if filename.endswith(".html")]:
            print(f"Compiling {htmlfile} into output/{htmlfile}")
            htmldata = open(os.path.join(STATIC_DIR, htmlfile)).read()
            output = master_template.replace("{contents}", htmldata)
            open(os.path.join(COMPILED_DIR, htmlfile), "w").write(output)

    @app.before_serving
    async def load_endpoints():
        """Load all API end points. This is run before Quart starts serving requests"""
        async with app.app_context():
            from . import endpoints

            # Regularly update the list of projects from LDAP
            app.add_background_task(config.get_projects_from_ldap)
            # Reset rate limits daily
            app.add_background_task(config.reset_rate_limits)
            # Fetch mailing lists hourly
            app.add_background_task(config.fetch_valid_lists)

    @app.after_serving
    async def shutdown():
        """Ensure a clean shutdown of the portal by stopping background tasks"""
        log.log("Shutting down selfserve portal...")
        app.background_tasks.clear()  # Clear repo polling etc

    return app
