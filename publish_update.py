"""Publish both validated desktop packages as one complete GitHub release."""
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = 'Marshallma289/token-dashboard-desktop'
BASE = f'https://api.github.com/repos/{REPOSITORY}'


def request(path, *, method='GET', data=None, content_type='application/json'):
    headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'], 'Accept': 'application/vnd.github+json', 'Content-Type': content_type, 'User-Agent': 'CodexTokenDashboard-Release'}
    payload = json.dumps(data).encode() if isinstance(data, dict) else data
    with urlopen(Request(path if path.startswith('https://uploads.github.com/') else BASE + path, headers=headers, data=payload, method=method), timeout=120) as response:
        return json.load(response)


def main():
    root = Path(sys.argv[1])
    commit = os.environ['GITHUB_SHA']
    if request('/branches/main')['commit']['sha'] != commit:
        print('Skipping superseded source commit')
        return
    manifests = list(root.rglob('*.build-manifest.json'))
    assert len(manifests) == 2, 'Both platform manifests are required'
    records = {}
    upload_files = []
    infos = []
    for path in manifests:
        info = json.loads(path.read_text(encoding='utf-8'))
        assert info['source_commit'] == commit and info['validation'] == 'unittest-and-node-check-passed'
        key = {('win32', 'x64'): 'windows-x64', ('darwin', 'arm64'): 'macos-arm64'}[(info['platform'], info['architecture'])]
        assert key not in records
        archive = path.with_name(path.name.replace('.build-manifest.json', '.zip'))
        assert archive.is_file()
        records[key] = {'archive': archive.name, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'size': archive.stat().st_size, 'manifest': path.name, 'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        upload_files.extend((archive, path))
        infos.append(info)
    assert infos[0]['version'] == infos[1]['version'] and infos[0]['source_digest'] == infos[1]['source_digest']
    version = infos[0]['version']
    number = int(os.environ['GITHUB_RUN_NUMBER'])
    tag = f"v{version}-build-{number}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    update = {'schema': 1, 'version': version, 'build_number': number, 'source_commit': commit, 'source_digest': infos[0]['source_digest'], 'packages': records}
    metadata = root / 'update.json'
    metadata.write_text(json.dumps(update, indent=2) + '\n', encoding='utf-8')
    upload_files.append(metadata)
    release = request('/releases', method='POST', data={'tag_name': tag, 'target_commitish': commit, 'name': f'Codex Token {version} · Build {number}', 'draft': True, 'prerelease': False, 'body': 'Windows x64 与 Apple Silicon macOS arm64 共用源码。\n支持在软件页面检查更新、一键下载更新并自动重启。\n统计数据库与个人设置保留；更新失败可恢复旧程序。\nmacOS 要求 14 或更新版本，当前采用临时签名，未公证。'})
    upload_url = release['upload_url'].split('{', 1)[0]
    for file in upload_files:
        request(upload_url + '?name=' + quote(file.name), method='POST', data=file.read_bytes(), content_type='application/zip' if file.suffix == '.zip' else 'application/json')
    # Only make a release visible after every package and checksum is uploaded.
    latest = request('/branches/main')['commit']['sha'] == commit
    request('/releases/' + str(release['id']), method='PATCH', data={'draft': False, 'make_latest': 'true' if latest else 'false'})
    print(release['html_url'])


if __name__ == '__main__':
    main()
