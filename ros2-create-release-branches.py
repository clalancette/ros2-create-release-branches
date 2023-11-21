import os
import sys
import tempfile
import time

import keyring
import git
import github
import requests
import yaml

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
    response = requests.get(ros2_repos_file_url)
    if not response.ok:
        raise Exception('Failed to fetch %s: %s' % (ros2_repos_file_url, str(response)))
    return yaml.safe_load(response.text)

def download_distribution_yaml(release_name: str):
    url = github_raw_from_url(ROSDISTRO_URL, f'/master/{release_name}/distribution.yaml')
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
                print('Package %s doc URL %s does not match source URL %s, skipping...' % (ros2_name, doc_url, source_url))
                continue

            release_url = None
            if 'release' in distro_info:
                release_url = distro_info['release']['url']

            ret[ros2_name] = (distro_name, release_url)
            break

    return ret

def create_source_branch(url: str, release_name: str):
    with tempfile.TemporaryDirectory() as tmpdirname:
        print('Cloning %s into %s' % (url, tmpdirname))
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
        print('Cloning %s into %s' % (distro_release_url, tmpdirname))
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
    pull = gh_repo.create_pull(title=f'Update {release_name} devel branch', head=new_branch_name, base='master', body=f'Update {release_name} devel branch')

def update_ros2_repos(ros2_repos: dict, release_name: str, gh: github.MainClass.Github):
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
    gh_repo = gh.get_repo(ros2_repos_github_name)

    pull = gh_repo.create_pull(title=f'Update {release_name} source branches', head=pr_branch_name, base=release_name, body=f'Update {release_name} source branches')

def update_distribution_yaml(distribution_yaml: dict, release_name: str, gh: github.MainClass.Github):
    # TODO(clalancette): implement
    with tempfile.TemporaryDirectory() as tmpdirname:
        pass

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
    #update_ros2_repos(ros2_repos, release_name, gh)

    # Open up a PR to rosdistro with the changes we just made to the distribution.yaml
    update_distribution_yaml(distribution_yaml, release_name, gh)

    return 0

if __name__ == '__main__':
    sys.exit(main())
