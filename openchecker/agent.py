import subprocess
from message_queue import consumer
from helper import read_config
from datetime import datetime
from exponential_backoff import post_with_backoff, completion_with_backoff
import json
import requests
import re
import time
import os
from ghapi.all import GhApi
import zipfile
import io
import logging
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s : %(message)s')

config = read_config('config/config.ini')

def get_licenses_name(data):
    return next(
        (license['meta']['title'] 
         for license in data.get('licenses', []) 
         if license.get('meta', {}).get('title')), 
        None
    )

def ruby_licenses(data):
    github_url_pattern = "https://github.com/"
    for item in data["analyzer"]["result"]["packages"]:
        declared_licenses = item["declared_licenses"]
        homepage_url = item.get('homepage_url', '')
        vcs_url = item.get('vcs_processed', {}).get('url', '').replace('.git', '')

        # 检查 declared_licenses 是否为空
        if not declared_licenses or len(declared_licenses)==0:
            # 优先检查 vcs_url 是否为 GitHub 地址
            if vcs_url.startswith(github_url_pattern):
                project_url = vcs_url
            elif homepage_url.startswith(github_url_pattern):
                project_url = homepage_url
            else:
                project_url = None
            # 如果找到了有效的 GitHub 地址，克隆仓库并调用 licensee
            if project_url:
                shell_script=f"""
                    project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                    if [ ! -e "$project_name" ]; then
                        GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                    fi
                    licensee detect "$project_name" --json
                    rm -rf $project_name > /dev/null
                """
                result, error = shell_exec(shell_script)
                if error == None:
                    try:
                        license_info = json.loads(result)
                        licenses_name = get_licenses_name(license_info)
                        item['declared_licenses'].append(licenses_name)
                    except json.JSONDecodeError as e:
                        logging.error(f"Failed to parse JSON from {project_url}: {e}")
                else:
                    logging.error("ruby_licenses job failed: {}, error: {}".format(project_url, error))
    return data

def dependency_checker_output_process(output):
    if not bool(output):
        return {}

    result = json.loads(output.decode('utf-8'))
    result = ruby_licenses(result)
    try:
        packages = result["analyzer"]["result"]["packages"]
        result = {"packages_all": [], "packages_with_license_detect": [], "packages_without_license_detect": []}

        for package in packages:
            result["packages_all"].append(package["purl"])
            license = package["declared_licenses"]
            if license != None and len(license) > 0:
                result["packages_with_license_detect"].append(package["purl"])
            else:
                result["packages_without_license_detect"].append(package["purl"])

    except Exception as e:
        logging.error(f"Error processing dependency-checker output: {e}")
        return {}

    return result

def shell_exec(shell_script, param=None):
    if param != None:
        process = subprocess.Popen(["/bin/bash", "-c", shell_script + " " + param], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
    else:
        process = subprocess.Popen([shell_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    shell_output, error = process.communicate()

    if process.returncode == 0:
        return shell_output, None
    else:
        return None, error

def request_url (url, payload):
    response = post_with_backoff(url=url, json=payload)

    if response.status_code == 200:
        logging.info("Request sent successfully.")
        return response.text
    else:
        logging.error(f"Failed to send request. Status code: {response.status_code}")
        return None

def check_readme_opensource(project_url):
    project_name = os.path.basename(project_url).replace('.git', '')

    if not os.path.exists(project_name):
        subprocess.run(["git", "clone", project_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    readme_file = os.path.join(project_name, "README.OpenSource")
    if os.path.isfile(readme_file):
        with open(readme_file, 'r', encoding='utf-8') as file:
            try:
                content = json.load(file)

                if isinstance(content, list):
                    required_keys = [
                        "Name", "License", "License File",
                        "Version Number", "Owner", "Upstream URL", "Description"
                    ]

                    all_entries_valid = True
                    for entry in content:
                        if not isinstance(entry, dict) or not all(key in entry for key in required_keys):
                            all_entries_valid = False
                            break

                    if all_entries_valid:
                        # return "The README.OpenSource file exists and is properly formatted.", None
                        return True, None
                    else:
                        # return None, "The README.OpenSource file exists and is not properly formatted."
                        return False, "The README.OpenSource file exists and is not properly formatted."

            except json.JSONDecodeError:
                return False, "README.OpenSource is not properly formatted."
    else:
        return False, "README.OpenSource does not exist."

def check_doc_content(project_url, type):
    project_name = os.path.basename(project_url).replace('.git', '')

    if not os.path.exists(project_name):
        subprocess.run(["git", "clone", project_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    dir_list = [project_name, project_name + '/' + 'doc', project_name + '/' + 'docs']

    def get_documents_in_directory(path):
        documents = []
        if not os.path.exists(path):
            return documents
        for item in os.listdir(path):
            full_path = os.path.join(path, item)
            if os.path.isfile(full_path) and item.endswith(('.md', '.markdown')):
                documents.append(full_path)
        return documents

    documents = []
    for dir in dir_list:
        documents.extend(get_documents_in_directory(dir))

    if type == "build-doc":
        do_link_include_check = True
        templates = """
            You are a professional programmer, please assess whether the provided text offers a thorough and in-depth introduction to the processes of software compilation and packaging.
            If the text segment introduce the software compilation and packaging completely, please return 'YES'; otherwise, return 'NO'.
            You need to ensure the accuracy of your answers as much as possible, and if unsure, please simply answer NO. Your response must not include other content.

            Text content as below:

            {text}

        """
    elif type == "api-doc":
        do_link_include_check = False
        templates = """
            You are a professional programmer, please assess whether the provided text offer a comprehensive introduction to the use of software API.
            If the text segment introduce the software API completely, please return 'YES'; otherwise, return 'NO'.
            You need to ensure the accuracy of your answers as much as possible, and if unsure, please simply answer NO. Your response must not include other content.

            Text content as below:

            {text}

        """
    else:
        logging.info("Unsupported type: {}".format(type))
        return [], None

    build_doc_file = []
    for document in documents:
        with open(document, 'r') as file:
            markdown_text = file.read()
            chunk_size = 3000
            chunks = [markdown_text[i:i+chunk_size] for i in range(0, len(markdown_text), chunk_size)]

        for _, chunk in enumerate(chunks):
            messages = [
                {
                    "role": "user",
                    "content": templates.format(text=chunk)
                }
            ]

            external_build_doc_link = "https://gitee.com/openharmony-tpc/docs/blob/master/OpenHarmony_har_usage.md"
            if do_link_include_check and external_build_doc_link.lower() in chunk.lower():
                return build_doc_file, None

            result = completion_with_backoff(messages=messages, temperature=0.2)
            if result == "YES":
                build_doc_file.append(document)
                return build_doc_file, None
    return build_doc_file, None

def check_release_content(project_url):
    owner_name = re.match(r"https://(?:github|gitee|gitcode).com/([^/]+)/", project_url).group(1)
    repo_name = re.sub(r'\.git$', '', os.path.basename(project_url))

    if "github.com" in project_url:
        api = GhApi(owner=owner_name, repo=repo_name)
        try:
            latest_release = api.repos.get_latest_release()
        except Exception as e:
            logging.error("Failed to get latest release for repo: {} \n Error: {}".format(project_url, e))
            return {"is_released": False, "signature_files": [], "release_notes": []}, "Not found"

        latest_release_url = latest_release["zipball_url"]

    elif "gitee.com" in project_url:
        url = f"https://gitee.com/api/v5/repos/{owner_name}/{repo_name}/releases/latest"
        try:
            response = requests.get(url)
            if response.status_code == 200:
                tag_name = response.json()["tag_name"]
                access_token = config["Gitee"]["access_key"]
                # latest_release_url = response.json()["assets"][0]["browser_download_url"]
                latest_release_url = f"https://gitee.com/api/v5/repos/{owner_name}/{repo_name}/zipball?access_token={access_token}&ref={tag_name}"
            else:
                logging.error("Failed to get latest release for repo: {} \n Error: {}".format(project_url, "Not found"))
                return {"is_released": False, "signature_files": [], "release_notes": []}, "Not found"
        except Exception as e:
            logging.error("Failed to get latest release for repo: {} \n Error: {}".format(project_url, e))
            return {"is_released": False, "signature_files": [], "release_notes": []}, "Not found"
    elif "gitcode.com" in project_url:
        # todo gitcode api
        pass
    else:
        logging.info("Failed to do release files check for repo: {} \n Error: {}".format(project_url, "Not supported platform."))
        return {"is_released": False, "signature_files": [], "release_notes": []}, "Not supported platform."

    response = requests.get(latest_release_url)
    if response.status_code != 200:
        return {"is_released": True, "signature_files": [], "release_notes": []}, "Failed to download release."

    signature_files = []
    changelog_files = []
    with zipfile.ZipFile(io.BytesIO(response.content), 'r') as zip_ref:
        signature_suffixes = ["*.asc", "*.sig", "*.cer", "*.crt", "*.pem", "*.sha256", "*.sha512"]
        signature_files = [ file for file in zip_ref.namelist() if any(file.lower().endswith(suffix) for suffix in signature_suffixes) ]

        changelog_names = ["changelog", "releasenotes", "release_notes"]
        changelog_files = [ file for file in zip_ref.namelist() if any(name in os.path.basename(file).lower() for name in changelog_names)]

    return {"is_released": True, "signature_files": signature_files, "release_notes": changelog_files}, None

def callback_func(ch, method, properties, body):

    logging.info(f"callback func called at {datetime.now()}")

    # message = json.loads(body.decode('utf-8'))
    message = body
    command_list = message.get('command_list')
    project_url = message.get('project_url')
    commit_hash = message.get("commit_hash")
    callback_url = message.get('callback_url')
    task_metadata = message.get('task_metadata')
    version_number = task_metadata.get("version_number", "None")
    logging.info(project_url)

    res_payload = {
        "command_list": command_list,
        "project_url": project_url,
        "task_metadata": task_metadata,
        "scan_results": {
        }
    }

    # download source code of the project
    shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone {project_url}
                fi

                cd "$project_name"

                if [ {version_number} != "None" ]; then
                    # 检查版本号是否在git仓库的tag中
                    if git tag | grep -q "^$version_number$"; then
                        # 切换到对应的tag
                        git checkout "$version_number"
                        if [ $? -eq 0 ]; then
                            echo "成功切换到标签 $version_number"
                        else
                            echo "切换到标签 $version_number 失败"
                        fi
                    fi
                fi
            """

    result, error = shell_exec(shell_script)

    if error == None:
        logging.info("download source code done: {}".format(project_url))
    else:
        logging.info("download source code failed: {}, error: {}".format(project_url, error))
        logging.info("put messages to dead letters: {}".format(body))
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    ## Generate the lock files, which would be used by the osv-scanner and ort tools.
    shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ -e "$project_name/package.json" ] && [ ! -e "$project_name/package-lock.json" ]; then
                    cd $project_name && npm install && rm -fr node_modules > /dev/null
                    echo "Generate lock files for $project_name with command npm."
                fi
                if [ -e "$project_name/oh-package.json5" ] && [ ! -e "$project_name/oh-package-lock.json5" ]; then
                    cd $project_name && ohpm install && rm -fr oh_modules > /dev/null
                    echo "Generate lock files for $project_name with command ohpm."
                fi
            """

    result, error = shell_exec(shell_script)

    if error == None:
        logging.info("Lock files generation job done: {}".format(result.decode('utf-8') if bool(result) else "No lock files generated."))
    else:
        logging.error("Lock files generation job failed: {}, error: {}".format(project_url, error))

    for command in command_list:
        if command == 'osv-scanner':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi

                # Rename oh-package-lock.json5 to package-lock.json make it readable by osv-scanner.
                if [ -f "$project_name/oh-package-lock.json5" ] && [! -f "$project_name/package-lock.json" ]; then
                    mv $project_name/oh-package-lock.json5 $project_name/package-lock.json  > /dev/null
                    rename_flag = 1
                fi

                # Outputs the results as a JSON object to stdout, with all other output being directed to stderr
                # - this makes it safe to redirect the output to a file.
                # shell_exec function return (None, error) when process.returncode is not 0, so we redirect output to a file and cat.
                osv-scanner --format json -r $project_name > $project_name/result.json
                cat $project_name/result.json
                # rm -rf $project_name > /dev/null

                if [ -v rename_flag ]; then
                    mv $project_name/package-lock.json $project_name/oh-package-lock.json5  > /dev/null
                fi
            """

            result, error = shell_exec(shell_script)

            # When osv-scanner tool specify the '--format json' option, only the scan results are output to the standard output.
            # All other information is redirected to the standard error output;
            # Hence, error values are not checked here.
            logging.info("osv-scanner job done: {}".format(project_url))
            osv_result = json.loads(result.decode('utf-8')) if bool(result) else {}
            res_payload["scan_results"]["osv-scanner"] = osv_result

        elif command == 'scancode':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                scancode -lc --json-pp scan_result.json $project_name --license-score 90 -n 4 > /dev/null
                cat scan_result.json
                rm -rf scan_result.json > /dev/null
                # rm -rf $project_name scan_result.json > /dev/null
            """

            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("scancode job done: {}".format(project_url))
                scancode_result = json.loads(result.decode('utf-8')) if bool(result) else {}
                res_payload["scan_results"]["scancode"] = scancode_result
            else:
                logging.error("scancode job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["scancode"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'binary-checker':
            result, error = shell_exec("./scripts/binary_checker.sh", project_url)

            if error == None:
                logging.info("binary-checker job done: {}".format(project_url))
                result = result.decode('utf-8') if bool(result) else ""
                data_list = result.split('\n')
                binary_file_list = []
                binary_archive_list = []
                for data in data_list[:-1]:
                    if "Binary file found:" in data:
                        binary_file_list.append(data.split(": ")[1])
                    elif "Binary archive found:" in data:
                        binary_archive_list.append(data.split(": ")[1])
                    else:
                        pass
                binary_result = {"binary_file_list": binary_file_list, "binary_archive_list": binary_archive_list}
                res_payload["scan_results"]["binary-checker"] = binary_result

            else:
                logging.error("binary-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["binary-checker"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'release-checker':

            result, error = check_release_content(project_url)

            if error == None:
                logging.info("release-checker job done: {}".format(project_url))
                res_payload["scan_results"]["release-checker"] = result
            else:
                logging.error("release-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["release-checker"] = {"error": error}

        elif command == 'url-checker':
            from urllib import request
            try:
                with request.urlopen(project_url) as file:
                    if file.status == 200 and file.reason == "OK":
                        logging.info("url-checker job done: {}".format(project_url))
                        url_result = {"url": project_url, "status": "pass", "error": "null"}
                    else:
                        logging.error("url-checker job failed: {}".format(project_url))
                        url_result = {"url": project_url, "status": "fail", "error": file.reason}
            except Exception as e:
                logging.error("gignature-checker job failed: {}, error: {}".format(project_url, e))
                url_result = {"error": e}
            res_payload["scan_results"]["url-checker"] = url_result

        elif command == 'sonar-scanner':

            pattern = r'https?://(?:www\.)?(github\.com|gitee\.com|gitcode\.com)/([^/]+)/([^/]+)\.git'
            match = re.match(pattern, project_url)
            if match:
                platform, organization, project = match.groups()
            else:
                platform, organization, project = "other", "default", "default"
            sonar_project_name = platform + "_" + organization + "_" + project

            sonar_config = config["SonarQube"]

            sonar_search_procet_api = f"http://{sonar_config['host']}:{sonar_config['port']}/api/projects/search"

            data = {"projects": sonar_project_name}
            auth = (sonar_config["username"], sonar_config["password"])
            is_exit = False

            try:
                response = requests.get(sonar_search_procet_api, auth=auth, params={"projects": sonar_project_name})
                if response.status_code == 200:
                    logging.info("Call sonarqube projects search api success: 200")
                    res = json.loads(response.text)
                    is_exit = True if res["paging"]["total"] > 0 else False
                else:
                    logging.error(f"Call sonarqube projects search api failed with status code: {response.status_code}")
            except requests.exceptions.RequestException as e:
                logging.error(f"Call sonarqube projects search api failed with error: {e}")

            if not is_exit:
                sonar_create_procet_api = f"http://{sonar_config['host']}:{sonar_config['port']}/api/projects/create"
                data = {"project": sonar_project_name, "name": sonar_project_name}

                try:
                    response = requests.post(sonar_create_procet_api, auth=auth, data=data)
                    if response.status_code == 200:
                        logging.info("Call sonarqube projects create api success: 200")
                    else:
                        logging.error(f"Call sonarqube projects create api failed with status code: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    logging.error(f"Call sonarqube projects create api failed with error: {e}")

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                cp -r $project_name ~/ && cd ~
                sonar-scanner \
                    -Dsonar.projectKey={sonar_project_name} \
                    -Dsonar.sources=$project_name \
                    -Dsonar.host.url=http://{sonar_config['host']}:{sonar_config['port']} \
                    -Dsonar.token={sonar_config['token']} \
                    -Dsonar.exclusions=**/*.java
                rm -rf $project_name > /dev/null
            """

            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("sonar-scanner finish scanning project: {}, report querying...".format(project_url))

                sonar_query_measures_api = f"http://{sonar_config['host']}:{sonar_config['port']}/api/measures/component"

                try:
                    # TODO: optimize this, waiting for sonarqube data processing
                    time.sleep(60)
                    response = requests.get(sonar_query_measures_api, auth=auth, params={"component": sonar_project_name, "metricKeys": "coverage,complexity,duplicated_lines_density,lines"})
                    if response.status_code == 200:
                        logging.info("Querying sonar-scanner report success: 200")
                        sonar_result = json.loads(response.text)
                        res_payload["scan_results"]["sonar-scanner"] = sonar_result
                    else:
                        logging.error(f"Querying sonar-scanner report failed with status code: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    logging.error(f"Querying sonar-scanner report failed with error: {e}")

                logging.info("sonar-scanner job done: {}".format(project_url))
            else:
                logging.error("sonar-scanner job failed: {}, error: {}".format(project_url, error))

        elif command == 'dependency-checker':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                ort -P ort.analyzer.allowDynamicVersions=true analyze -i $project_name -o $project_name -f JSON > /dev/null
                cat $project_name/analyzer-result.json
                # rm -rf $project_name > /dev/null
            """
            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("dependency-checker job done: {}".format(project_url))
                res_payload["scan_results"]["dependency-checker"] = dependency_checker_output_process(result)
            else:
                logging.error("dependency-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["dependency-checker"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'readme-checker':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                find "$project_name" -type f \( -name "README*" -o -name ".github/README*" -o -name "docs/README*" \) -print
            """

            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("readme-checker job done: {}".format(project_url))
                res_payload["scan_results"]["readme-checker"] = {"readme_file": result.decode('utf-8').split('\n')[:-1]} if bool(result) else {}
            else:
                logging.error("readme-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["readme-checker"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'maintainers-checker':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                find "$project_name" -type f \( -iname "MAINTAINERS*" -o -iname "COMMITTERS*" -o -iname "OWNERS*" -o -iname "CODEOWNERS*" \) -print
            """

            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("maintainers-checker job done: {}".format(project_url))
                res_payload["scan_results"]["maintainers-checker"] = {"maintainers_file": result.decode('utf-8').split('\n')[:-1]} if bool(result) else {}
            else:
                logging.error("maintainers-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["maintainers-checker"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'readme-opensource-checker':
            result, error  = check_readme_opensource(project_url)
            if error == None:
                logging.info("readme-opensource-checker job done: {}".format(project_url))
                res_payload["scan_results"]["readme-opensource-checker"] = {"readme-opensource-checker": result} if bool(result) else {}
            else:
                logging.error("readme-opensource-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["readme-opensource-checker"] = {"error":error}

        elif command == 'build-doc-checker':
            result, error  = check_doc_content(project_url, "build-doc")
            if error == None:
                logging.info("build-doc-checker job done: {}".format(project_url))
                res_payload["scan_results"]["build-doc-checker"] = {"build-doc-checker": result} if bool(result) else {}
            else:
                logging.error("build-doc-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["build-doc-checker"] = {"error":error}

        elif command == 'api-doc-checker':
            result, error  = check_doc_content(project_url, "api-doc")
            if error == None:
                logging.info("api-doc-checker job done: {}".format(project_url))
                res_payload["scan_results"]["api-doc-checker"] = {"api-doc-checker": result} if bool(result) else {}
            else:
                logging.error("api-doc-checker job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["api-doc-checker"] = {"error":error}

        elif command == 'languages-detector':

            shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi
                github-linguist $project_name --breakdown --json
            """

            result, error = shell_exec(shell_script)

            if error == None:
                logging.info("languages-detector job done: {}".format(project_url))
                res_payload["scan_results"]["languages-detector"] = json.dumps(result.decode("utf-8")) if bool(result) else {}
            else:
                logging.error("languages-detector job failed: {}, error: {}".format(project_url, error))
                res_payload["scan_results"]["languages-detector"] = {"error": json.dumps(error.decode("utf-8"))}

        elif command == 'changed-files-since-commit-detector':
            if commit_hash is None:
                print("Fail to get commit hash from message body!")
                continue

            context_path = os.getcwd()
            try:
                repository_path = os.path.join(context_path, os.path.splitext(os.path.basename(urlparse(project_url).path))[0])
                os.chdir(repository_path)
                print(f"change os path to git repository directory: {repository_path}")
            except OSError as e:
                print(f"failed to change os path to git repository directory: {e}")

            # type can be: [(A|C|D|M|R|T|U|X|B)…​[*]]
            # Added (A), Copied (C), Deleted (D), Modified (M), Renamed (R),
            # have their type (i.e. regular file, symlink, submodule, …​) changed (T),
            # are Unmerged (U), are Unknown (X), or have had their pairing Broken (B).
            # Reference to git official docs:
            # https://git-scm.com/docs/git-diff#Documentation/git-diff.txt---diff-filterACDMRTUXB82308203
            def get_diff_files(type="ACDMRTUXB"):
                try:
                    result = subprocess.check_output(
                       ["git", "diff", "--name-only", f"--diff-filter={type}", f"{commit_hash}..HEAD"],
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    return result.strip().split("\n") if result else []
                except subprocess.CalledProcessError as e:
                    print(f"failed to get {type} files: {e.output}")
                    return []

            changed_files = get_diff_files()
            new_files = get_diff_files("A")
            rename_files = get_diff_files("R")
            deleted_files = get_diff_files("D")
            modified_files = get_diff_files("M")

            os.chdir(context_path)

            res_payload["scan_results"]["changed-files-since-commit-detector"] = {
                "changed_files": changed_files,
                "new_files": new_files,
                "rename_files": rename_files,
                "deleted_files": deleted_files,
                "modified_files": modified_files
                }

        elif command == 'oat-scanner':
            # https://gitee.com/openharmony-sig/tools_oat
            shell_script = f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ ! -e "$project_name" ]; then
                    GIT_ASKPASS=/bin/true git clone --depth=1 {project_url} > /dev/null
                fi                
                if [ ! -f "$project_name/OAT.xml" ]; then
                    echo "OAT.xml not found in the project root directory."
                    exit 1   
                fi
                java -jar ohos_ossaudittool-2.0.0.jar -mode s -s $project_name   -r $project_name/oat_out -n $project_name > /dev/null            
                report_file="$project_name/oat_out/single/PlainReport_$project_name.txt"
                if [ -f "$report_file" ]; then                    
                    cat "$report_file"                                    
                fi                        
            """
            result, error = shell_exec(shell_script)
            
            if error == None:
                logging.info("The oat scan was successful. Analyzing the report.: {}".format(project_url))
            else:
                logging.error("oat-scanner job failed: {}, error: {}".format(project_url, error))

            def parse_oat_txt_to_json(txt):
                try:
                    de_str = txt.decode('unicode_escape')
                    result = {}
                    lines = de_str.splitlines()
                    current_section = None
                    pattern = r"Name:\s*(.+?)\s*Content:\s*(.+?)\s*Line:\s*(\d+)\s*Project:\s*(.+?)\s*File:\s*(.+)"

                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        total_count_match = re.search(r"^(.*) Total Count:\s*(\d+)", line, re.MULTILINE)
                        category_name = total_count_match.group(1).strip() if total_count_match else "Unknown"

                        if 'Total Count' in line:
                            current_section = category_name
                            total_count = int(line.split(":")[1].strip())
                            result[current_section] = {"total_count": total_count, "details": []}
                        elif line.startswith("Name:"):
                            matches = re.finditer(pattern, line)
                            for match in matches:
                                entry = {
                                    "name": match.group(1).strip(),
                                    "content": match.group(2).strip(),
                                    "line": int(match.group(3).strip()),
                                    "project": match.group(4).strip(),
                                    "file": match.group(5).strip(),
                                }
                            if current_section and "details" in result[current_section]:
                                result[current_section]["details"].append(entry)
                    return result
                except Exception as e:
                    logging.error("parse_oat_txt error: {}".format(e))
                    return e

            res_payload["scan_results"]["oat-scanner"] = {}
            if not result:
                logging.info("{} OAT.xml not found".format(project_url))
                # OAT.xml not found status_code: 404, success code:200, error code:500
                res_payload["scan_results"]["oat-scanner"] = {
                    "status_code": 404,
                    "error": "OAT.xml not found"
                }
            else:
                parse_res = parse_oat_txt_to_json(result)
                if error is None:
                    logging.info("oat-scanner job done: {}".format(project_url))
                    res_payload["scan_results"]["oat-scanner"] = parse_res
                    res_payload["scan_results"]["oat-scanner"]["status_code"] = 200
                else:
                    logging.error("oat-scanner job failed: {}, error: {}".format(project_url, error))
                    res_payload["scan_results"]["oat-scanner"] = {
                        "status_code": 500,
                        "error": json.dumps(error.decode("utf-8"))
                    }
        else:
            logging.info(f"Unknown command: {command}")

    # remove source code of the project
    shell_script=f"""
                project_name=$(basename {project_url} | sed 's/\.git$//') > /dev/null
                if [ -e "$project_name" ]; then
                    rm -rf $project_name > /dev/null
                fi
            """

    result, error = shell_exec(shell_script)

    if error == None:
        logging.info("remove source code done: {}".format(project_url))
    else:
        logging.error("remove source code failed: {}, error: {}".format(project_url, error))

    if callback_url != None and callback_url != "":
        try:
            response = request_url(callback_url, res_payload)
            logging.info(f"Callback response: {response}")
        except Exception as e:
            logging.error("Error happened when request to callback url: {}".format(e))
            logging.error("put messages to dead letters: {}".format(body))
            # logging.error("checker results: {}".format(res_payload))
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

    ch.basic_ack(delivery_tag=method.delivery_tag)

if __name__ == "__main__":
    consumer(config["RabbitMQ"], "opencheck", callback_func)
    logging.info('Agents server ended.')
