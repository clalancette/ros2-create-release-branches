# Copyright (c) 2023, Open Source Robotics Foundation
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of the Willow Garage, Inc. nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# This is a script to do the grunt work of creating source branches for a new distribution.
# The script assumes a few things:
#
# 1. That the user has a GitHub API key with all 'repo' permissions in the keyring.  Further,
#    that API key must be available as the service name 'github-api-token' with the username 'may-open-prs',
#    and the token set as the password.  This can be setup by creating a new token in the GitHub UI,
#    and then running:
#
#       keyring set github-api-token may-open-prs
#
#   and pasting in the token as the password when prompted.
#
# 2. That the user has write access to a bunch of ROS 2 infrastructure.  In particular:
#    * https://github.com/ros2/ros2
#    * https://github.com/ros/rosdistro
#    * https://github.com/ros2-gbp/* (basically all of the release repositories for the core)
#
# 3. That a migration from Rolling to the new distribution name has already been run for the *binaries*.
#    In other words, that https://github.com/ros/rosdistro/blob/master/migration-tools/migrate-rosdistro.py
#    has already been run, that the <release_name>/distribution.yaml file is available in rosdistro, and that
#    a "jazzy" stanza exists in the YAML in each of the release repositories.
#
# Assuming all of the above is true, this script can be run with:
#
#   python3 ros2-create-release-branches.py <releasename>
#
# It will then go through and update the ros2.repos file, the distribution.yaml file, and the tracks.yaml
# file in each release repository and update the appropriate spots with a new branch.  It will also go and
# create a new source branch for the release in each source repository listed in ros2.repos.

import logging
import os
import sys
import tempfile
import time

import keyring
import git
import github
import requests
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('ros2-create-release-branches')

#ROS2_REPOS_URL = 'https://github.com/ros2/ros2'
ROS2_REPOS_URL = 'https://github.com/clalancette/ros2'

#ROSDISTRO_URL = 'https://github.com/ros/rosdistro'
ROSDISTRO_URL = 'https://github.com/clalancette/myrosdistro'

def github_name_from_url(url: str):
    # Given something like https://github.com/ros2/ros2, this will produce 'ros2/ros2'
    if not url.startswith('https://github.com/'):
        raise Exception('URL must start with https://github.com/')
    return url.removesuffix('.git').removeprefix('https://github.com/')

def github_raw_from_url(url: str, filename: str):
    # Given something like https://github.com/ros2/ros2 and '/rolling/ros.repos',
    # this will produce 'https://raw.githubusercontent.com/ros2/ros2/rolling/ros2.repos'
    if not url.startswith('https://github.com/'):
        raise Exception('URL must start with https://github.com/')
    return url.replace('https://github.com', 'https://raw.githubusercontent.com') + filename

def download_ros2_repos():
    ros2_repos_file_url = github_raw_from_url(ROS2_REPOS_URL, '/rolling/ros2.repos')

    logger.info(f'Downloading ros2.repos file from {ros2_repos_file_url}')

    response = requests.get(ros2_repos_file_url)
    if not response.ok:
        raise Exception('Failed to fetch %s: %s' % (ros2_repos_file_url, str(response)))
    return yaml.safe_load(response.text)

def download_distribution_yaml(release_name: str):
    url = github_raw_from_url(ROSDISTRO_URL, f'/master/{release_name}/distribution.yaml')

    logger.info(f'Downloading distribution.yaml file from {url}')

    response = requests.get(url)
    if not response.ok:
        raise Exception('Failed to fetch %s: %s' % (url, str(response)))
    return yaml.safe_load(response.text)

def map_ros2_repos_to_distribution_yaml(ros2_repos: dict, distribution_yaml: dict):
    ret = {}

    for ros2_name, ros2_repo_info in ros2_repos['repositories'].items():
        ros2_url = ros2_repo_info['url'].removesuffix('.git')

        # TODO(clalancette): We could speed this up by removing items from the dict
        # as we find them
        for distro_name, distro_info in distribution_yaml['repositories'].items():
            doc_url = None
            if 'doc' in distro_info and distro_info['doc']['url'].removesuffix('.git') == ros2_url:
                doc_url = distro_info['doc']['url']

            source_url = None
            if 'source' in distro_info and distro_info['source']['url'].removesuffix('.git') == ros2_url:
                source_url = distro_info['source']['url']

            if doc_url is None and source_url is None:
                # No match found, continue looking
                continue

            if doc_url != source_url:
                # The URLs were both present, but different.  This shouldn't happen
                logger.warning('Package %s doc URL %s does not match source URL %s, skipping...' % (ros2_name, doc_url, source_url))
                continue

            release_url = None
            if 'release' in distro_info:
                release_url = distro_info['release']['url']

            ret[ros2_name] = (distro_name, release_url)
            break

    return ret

def create_source_branch(url: str, release_name: str):
    logger.info(f'Creating source branch {release_name} from "rolling" on {url}')

    with tempfile.TemporaryDirectory() as tmpdirname:
        gitrepo = git.Repo.clone_from(url, tmpdirname)
        gitrepo.git.checkout('rolling')

        # Create a new branch corresponding to this releases' name
        # TODO(clalancette): Check if branch already exists
        releasebranch = gitrepo.create_head(release_name)
        releasebranch.checkout()
        gitrepo.git.push('--set-upstream', gitrepo.remote(), gitrepo.head.ref)

def update_distribution_yaml(distribution_yaml: dict, distro_key_name: str, release_name: str):
    if 'doc' in distribution_yaml['repositories'][distro_key_name]:
        distribution_yaml['repositories'][distro_key_name]['doc']['version'] = release_name
    if 'source' in distribution_yaml['repositories'][distro_key_name]:
        distribution_yaml['repositories'][distro_key_name]['source']['version'] = release_name

def update_tracks_yaml(distro_release_url: str, release_name: str, gh: github.MainClass.Github):
    new_branch_name = f'{release_name}/update-devel-branch'

    with tempfile.TemporaryDirectory() as tmpdirname:
        gitrepo = git.Repo.clone_from(distro_release_url, tmpdirname)
        gitrepo.git.checkout('master')

        branch = gitrepo.create_head(new_branch_name)
        branch.checkout()

        with open(os.path.join(tmpdirname, 'tracks.yaml'), 'r') as infp:
            local_tracks_data = infp.read()
        local_tracks_yaml = yaml.safe_load(local_tracks_data)
        local_tracks_yaml['tracks'][release_name]['devel_branch'] = release_name
        with open(os.path.join(tmpdirname, 'tracks.yaml'), 'w') as outfp:
            yaml.dump(local_tracks_yaml, outfp)

        gitrepo.git.add(A=True)
        gitrepo.index.commit(f'Update {release_name} devel branch')
        gitrepo.git.push('--set-upstream', gitrepo.remote(), gitrepo.head.ref)

    github_repo_name = github_name_from_url(distro_release_url)

    gh_repo = gh.get_repo(github_repo_name)

    # TODO(clalancette): Make the title and body more informative
    pull = gh_repo.create_pull(title=f'Update {release_name} devel branch', head=new_branch_name, base='master', body=f'Update {release_name} devel branch')
    logger.info(f'Opened PR to update tracks.yaml devel_branch at {pull.html_url}')

def ros2_repos_open_pr(ros2_repos: dict, release_name: str, gh: github.MainClass.Github):
    pr_branch_name = f'{release_name}-initial-branches'

    with tempfile.TemporaryDirectory() as tmpdirname:
        gitrepo = git.Repo.clone_from(ROS2_REPOS_URL, tmpdirname)
        gitrepo.git.checkout('rolling')

        # Create a new branch in ROS2_REPOS_URL corresponding to this releases' name
        # TODO(clalancette): Check if branch already exists
        releasebranch = gitrepo.create_head(release_name)
        releasebranch.checkout()
        gitrepo.git.push('--set-upstream', gitrepo.remote(), gitrepo.head.ref)

        # Push a branch with the changes we just made
        branch = gitrepo.create_head(pr_branch_name)
        branch.checkout()
        with open(os.path.join(tmpdirname, 'ros2.repos'), 'w') as outfp:
            yaml.dump(ros2_repos, outfp)
        gitrepo.git.add(A=True)
        gitrepo.index.commit(f'Update {release_name} source branches')
        gitrepo.git.push('--set-upstream', gitrepo.remote(), gitrepo.head.ref)

    # Open up a PR to the ROS2_REPOS_URL with the changes we just made to ros2.repos
    ros2_repos_github_repo_name = github_name_from_url(ROS2_REPOS_URL)
    gh_repo = gh.get_repo(ros2_repos_github_repo_name)

    # TODO(clalancette): Make the title and body more informative
    pull = gh_repo.create_pull(title=f'Update {release_name} source branches', head=pr_branch_name, base=release_name, body=f'Update {release_name} source branches')
    logger.info(f'Opened PR to update ros2.repos at {pull.html_url}')

def distribution_yaml_open_pr(distribution_yaml: dict, release_name: str, gh: github.MainClass.Github):
    pr_branch_name = f'{release_name}-update'

    with tempfile.TemporaryDirectory() as tmpdirname:
        gitrepo = git.Repo.clone_from(ROSDISTRO_URL, tmpdirname)
        gitrepo.git.checkout('master')

        branch = gitrepo.create_head(pr_branch_name)
        branch.checkout()
        with open(os.path.join(tmpdirname, release_name, 'distribution.yaml'), 'w') as outfp:
            outfp.write('%YAML 1.1\n')
            outfp.write('# ROS distribution file\n')
            outfp.write('# see REP 143: http://ros.org/reps/rep-0143.html\n')
            outfp.write('---\n')
            yaml.dump(distribution_yaml, outfp)
        gitrepo.git.add(A=True)
        gitrepo.index.commit(f'Update {release_name} information')
        gitrepo.git.push('--set-upstream', gitrepo.remote(), gitrepo.head.ref)

    # Open up a PR to the ROSDISTRO_URL with the changes we just made to distribution.yaml
    rosdistro_repo_name = github_name_from_url(ROSDISTRO_URL)

    gh_repo = gh.get_repo(rosdistro_repo_name)

    # TODO(clalancette): Make the title and body more informative
    pull = gh_repo.create_pull(title=f'Update {release_name}', head=pr_branch_name, base='master', body=f'Update {release_name}')
    logger.info(f'Opened PR to update distribution.yaml at {pull.html_url}')

def main():
    if len(sys.argv) != 2:
        print('Usage: %s <release-name>' % (sys.argv[0]))
        return 1

    release_name = sys.argv[1]

    key = keyring.get_password('github-api-token', 'may-open-prs')
    if key is None:
        raise RuntimeError('Failed to get GitHub API key')

    gh = github.Github(key)

    # Download the ros2.repos file
    ros2_repos = download_ros2_repos()

    # Download the distribution.yaml file corresponding to this release
    distribution_yaml = download_distribution_yaml(release_name)

    ros2_key_to_distro_key = map_ros2_repos_to_distribution_yaml(ros2_repos, distribution_yaml)

    # For every repository in the ros2.repos file, we have to do a few things:
    # 1.  Make a branch on the source repository that corresponds to the passed-in name.
    # 2.  Update ros2.repos with that new branch name.
    # 3.  Update the 'doc' and 'source' sections in distribution.yaml file with that new branch name (for instance, update https://github.com/ros/rosdistro/blob/master/iron/distribution.yaml).
    # 4.  Update the 'tracks.yaml' file in the individual release repositories that corresponds to the passed-in name.

    # These are repositories that we should *not* create new branches for
    SKIPLIST = ('eProsima/Fast-CDR', 'eProsima/Fast-DDS', 'eProsima/foonathan_memory_vendor', 'eclipse-cyclonedds/cyclonedds', 'eclipse-iceoryx/iceoryx', 'osrf/osrf_pycommon', 'ros/urdfdom', 'ros/urdfdom_headers')

    for name, repo_info in ros2_repos['repositories'].items():
        if name in SKIPLIST:
            continue

        # Step 1
        create_source_branch(repo_info['url'], release_name)

        # Step 2
        repo_info['version'] = release_name

        # Step 3
        update_distribution_yaml(distribution_yaml, ros2_key_to_distro_key[name][0], release_name)

        # Step 4
        update_tracks_yaml(ros2_key_to_distro_key[name][1], release_name, gh)

    # Open a PR to ros2/ros2 with the changes we just made to ros2.repos
    ros2_repos_open_pr(ros2_repos, release_name, gh)

    # Open up a PR to rosdistro with the changes we just made to the distribution.yaml
    distribution_yaml_open_pr(distribution_yaml, release_name, gh)

    return 0

if __name__ == '__main__':
    sys.exit(main())
