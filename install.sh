#!/bin/bash
# Apple Silicon macOS 14+, native system tools only. Install through /bin/bash.
# curl -fsSL https://github.com/Marshallma289/token-dashboard-desktop/releases/latest/download/install.sh | /bin/bash
set -Eeuo pipefail
trap 'printf "安装失败：第 %s 行。\n" "$LINENO" >&2' ERR
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=C
umask 077
REPO=Marshallma289/token-dashboard-desktop
LATEST_UPDATE="https://github.com/$REPO/releases/latest/download/update.json"
RELEASE_BASE=
TARGET="$HOME/Applications/CodexTokenDesktop.app"
LAUNCH=1
CHECK=0
STAGE=
BACKUP=
COMMITTED=0
MOVED=0
CANDIDATE_ID=
LOCK=
LOCK_ID=
fail() { printf '安装失败：%s\n' "$*" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-dir) [ "$#" -ge 2 ] || fail '--install-dir 缺少路径'; TARGET=$2; shift 2 ;;
        --no-launch) LAUNCH=0; shift ;;
        --check-only) CHECK=1; LAUNCH=0; shift ;;
        --help|-h) printf '%s\n' '用法：install.sh [--install-dir /完整路径/名称.app] [--no-launch] [--check-only]'; exit 0 ;;
        *) fail "未知参数：$1" ;;
    esac
done
[ "$(/usr/bin/uname -s)" = Darwin ] || fail '仅支持 macOS'
[ "$(/usr/bin/uname -m)" = arm64 ] || fail '仅支持原生 Apple Silicon arm64；请退出 Rosetta 终端'
OS_VERSION=$(/usr/bin/sw_vers -productVersion)
[ "${OS_VERSION%%.*}" -ge 14 ] || fail '需要 macOS 14 或更新版本'
[ "$(/usr/bin/id -u)" -ne 0 ] || fail '请以普通用户运行，不要使用 sudo'
case "$TARGET" in
    /*.app) ;;
    *) fail '安装目标必须是以 .app 结尾的绝对路径' ;;
esac
case "$TARGET" in
    *$'\n'*|*$'\r'*|*\\*|*//*|*/./*|*/../*|*/.|*/..|/System/*|/usr/*|/bin/*|/sbin/*|/dev/*|/private/*|/etc/*|/var/*|*/AppTranslocation/*)
        fail '安装目标不是安全的固定路径' ;;
esac
PARENT=${TARGET%/*}
[ -n "$PARENT" ] && [ "$PARENT" != / ] || fail '不能安装到根目录'
# Inspect every existing component before mkdir; physical resolution must agree.
validate_path() {
    local part current= rest=${1#/}
    while [ -n "$rest" ]; do
        part=${rest%%/*}
        if [ "$rest" = "$part" ]; then rest=; else rest=${rest#*/}; fi
        [ -n "$part" ] && [ "$part" != . ] && [ "$part" != .. ] || fail '安装路径含无效组件'
        current="$current/$part"
        [ ! -L "$current" ] || fail "安装路径经过链接：$current"
        if [ -e "$current" ] && [ -n "$rest" ]; then [ -d "$current" ] || fail '安装路径组件不是目录'; fi
    done
}
validate_path "$TARGET"
/bin/mkdir -p "$PARENT"
[ "$(cd "$PARENT" && /bin/pwd -P)" = "$PARENT" ] || fail '安装目录的实际路径不一致'
[ -w "$PARENT" ] || fail '安装目录不可写'
plist_get() { /usr/bin/plutil -extract "$2" raw -o - "$1"; }
get() {
    /usr/bin/osascript -l JavaScript -e '
ObjC.import("Foundation");
function run(args) {
    var bytes = $.NSData.dataWithContentsOfFile(args[0]);
    var text = $.NSString.alloc.initWithDataEncoding(bytes, $.NSUTF8StringEncoding);
    var value = JSON.parse(ObjC.unwrap(text));
    args[1].split(".").forEach(function(key) {
        if (value === null || typeof value !== "object" || !Object.prototype.hasOwnProperty.call(value, key))
            throw new Error("Missing JSON field: " + args[1]);
        value = value[key];
    });
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean")
        throw new Error("JSON field must be a scalar: " + args[1]);
    return String(value);
}' "$1" "$2"
}
identity() {
    [ -d "$1" ] && [ ! -L "$1" ] && [ ! -L "$1/Contents" ] && [ ! -L "$1/Contents/Resources" ] && [ ! -L "$1/Contents/MacOS" ] && [ -f "$1/Contents/Info.plist" ] && [ ! -L "$1/Contents/Info.plist" ] &&
    [ "$(plist_get "$1/Contents/Info.plist" CFBundleIdentifier)" = local.codextokendashboard ] &&
    [ "$(plist_get "$1/Contents/Info.plist" CFBundleExecutable)" = CodexTokenDesktop ] &&
    [ -f "$1/Contents/Resources/build-info.json" ] &&
    [ -x "$1/Contents/MacOS/CodexTokenDesktop" ]
}
not_running() {
    if /bin/ps -axo command= | /usr/bin/awk -v p="$TARGET/Contents/MacOS/CodexTokenDesktop" '
        index($0,p)==1 && (length($0)==length(p) || substr($0,length(p)+1,1)==" ") { found=1 }
        END { exit(found ? 0 : 1) }'; then fail '应用正在运行，请关闭后再安装'; fi
}
if [ -e "$TARGET" ]; then identity "$TARGET" || fail '目标已存在且不是可识别的 CodexTokenDesktop 应用'; /usr/bin/codesign --verify --deep --strict "$TARGET" || fail '现有应用签名校验失败，已保留原目录'; fi
not_running
# The application's updater leaves its lock behind after the helper exits.
# Only reclaim a well-formed lock for this recognized app with a dead PID.
check_update_lock() {
UPDATE_LOCK="$PARENT/.${TARGET##*/}.update.lock"
if [ -e "$UPDATE_LOCK" ] || [ -L "$UPDATE_LOCK" ]; then
    [ -e "$TARGET" ] && identity "$TARGET" || fail '未知应用更新锁，需先检查该锁文件'
    [ -f "$UPDATE_LOCK" ] && [ ! -L "$UPDATE_LOCK" ] && [ "$(/usr/bin/stat -f %z "$UPDATE_LOCK")" -le 65536 ] || fail '应用更新锁不安全'
    UPDATE_PID=$(get "$UPDATE_LOCK" pid) || fail '应用更新锁已损坏'
    [[ "$UPDATE_PID" =~ ^[1-9][0-9]{0,9}$ ]] && [ "$UPDATE_PID" -le 2147483647 ] || fail '应用更新锁 PID 无效'
    if /bin/kill -0 "$UPDATE_PID" 2>/dev/null || /bin/ps -p "$UPDATE_PID" -o pid= >/dev/null 2>&1; then fail '应用更新程序正在运行，请稍后重试'; fi
    UPDATE_LOCK_ID=$(/usr/bin/stat -f '%d:%i' "$UPDATE_LOCK")
    validate_path "$UPDATE_LOCK"
    [ "$(/usr/bin/stat -f '%d:%i' "$UPDATE_LOCK")" = "$UPDATE_LOCK_ID" ] || fail '更新锁发生变化'
    /bin/rm -f -- "$UPDATE_LOCK"
fi
}
check_update_lock
cleanup() {
    local result=$?
    set +e
    trap - EXIT ERR HUP INT TERM
    if [ "$COMMITTED" -eq 0 ] && [ "$MOVED" -eq 1 ] && [ -z "$BACKUP" ]; then
        if [ -d "$TARGET" ] && [ ! -L "$TARGET" ] && identity "$TARGET" &&
            [ "$(/usr/bin/stat -f '%d:%i' "$TARGET")" = "$CANDIDATE_ID" ] &&
            [ "$(cd "$TARGET" && /bin/pwd -P)" = "$TARGET" ] && [ ! -e "$STAGE/failed.app" ]; then
            /bin/mv "$TARGET" "$STAGE/failed.app" || { printf '候选应用无法移回暂存目录：%s\n' "$TARGET" >&2; result=1; }
        else
            printf '首次安装失败，目标发生变化，已保留：%s\n' "$TARGET" >&2; result=1
        fi
    fi
    if [ "$COMMITTED" -eq 0 ] && [ -n "$BACKUP" ] && [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ]; then
        if [ -e "$TARGET" ]; then
            if identity "$TARGET" && [ ! -e "$STAGE/failed.app" ]; then /bin/mv "$TARGET" "$STAGE/failed.app" || result=1;
            else printf '保留备份，请手动恢复：%s\n' "$BACKUP" >&2; result=1; fi
        fi
        if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
            /bin/mv "$BACKUP" "$TARGET" || { printf '恢复失败，旧应用保留在：%s\n' "$BACKUP" >&2; result=1; }
        fi
    fi
    # Recursive deletion is limited to our mktemp directory, never backups.
    if [ -n "$STAGE" ] && [ -d "$STAGE" ] && [ ! -L "$STAGE" ] &&
        [ "${STAGE%/*}" = "$PARENT" ] && [ "$(cd "$STAGE" && /bin/pwd -P)" = "$STAGE" ]; then
        case "${STAGE##*/}" in .codex-token-install.????????) /bin/rm -rf -- "$STAGE" ;; esac
    fi
    if [ -n "$LOCK" ] && [ -d "$LOCK" ] && [ ! -L "$LOCK" ] &&
        [ "$(/usr/bin/stat -f '%d:%i' "$LOCK")" = "$LOCK_ID" ]; then /bin/rmdir "$LOCK" 2>/dev/null || true; fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
LOCK="$PARENT/.${TARGET##*/}.terminal-install.lock"
/bin/mkdir "$LOCK" || { LOCK=; fail '另一个安装正在进行，或安装锁目录已存在'; }
LOCK_ID=$(/usr/bin/stat -f '%d:%i' "$LOCK")
STAGE=$(/usr/bin/mktemp -d "$PARENT/.codex-token-install.XXXXXXXX")
# JXA here only parses data and ZIP records; it does not automate any UI.
/bin/cat > "$STAGE/verify.js" <<'JXA'
ObjC.import('Foundation');
function bad(s) { throw new Error(s); }
function str(x) { return ObjC.unwrap(x); }
function dataText(d) {
    var s = $.NSString.alloc.initWithDataEncoding(d, $.NSUTF8StringEncoding);
    if (!s) bad('Invalid UTF-8');
    return str(s);
}
function json(p) { return JSON.parse(dataText($.NSData.dataWithContentsOfFile(p))); }
function bytes(d) {
    var s = str(d.base64EncodedStringWithOptions(0)), chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/', out = [], n = 0, bits = 0;
    for (var i=0;i<s.length;i++) {
        if (s[i]==='=') break;
        var v=chars.indexOf(s[i]); if (v<0) bad('Invalid base64');
        n=(n<<6)|v; bits+=6;
        if (bits>=8) { bits-=8; out.push((n>>>bits)&255); }
    }
    return out;
}
function utf8(a) {
    // Lossless UTF-8 decoding, including non-ASCII bundle names.
    var s=''; for (var i=0;i<a.length;i++) s+='%'+('0'+a[i].toString(16)).slice(-2);
    return decodeURIComponent(s);
}
function task(exe,args) {
    var t=$.NSTask.alloc.init, p=$.NSPipe.pipe;
    t.launchPath=exe; t.arguments=args; t.standardOutput=p;
    t.launch;
    var output=p.fileHandleForReading.readDataToEndOfFile;
    t.waitUntilExit;
    if (t.terminationStatus!==0) bad('Native command failed: '+exe);
    return dataText(output);
}
function validPath(p) {
    return typeof p==='string' && p.length>0 && !/[\\:\x00-\x1f\x7f]/.test(p) && p[0]!=='/' && p.split('/').every(function(x){return x!=='' && x!=='.' && x!=='..';});
}
function checkInfo(u,m) {
    if(typeof u.build_number!=='number' || u.build_number<0 || Math.floor(u.build_number)!==u.build_number || u.schema!==1 || !/^\d+\.\d+\.\d+$/.test(u.version) || !/^[0-9a-f]{40}$/.test(u.source_commit) || !/^[0-9a-f]{64}$/.test(u.source_digest)) bad('Invalid update metadata');
    if(m.version!==u.version || m.source_commit!==u.source_commit || m.source_digest!==u.source_digest || m.architecture!=='arm64' || m.platform!=='darwin') bad('Manifest platform/version/source mismatch');
}
function run(args) {
    var u=json(args[3]), m=json(args[2]); checkInfo(u,m);
    if(!Array.isArray(m.files) || m.files.length===0 || m.files.length>20000) bad('Invalid manifest file count');
    var declared=Object.create(null);
    m.files.forEach(function(r){
        if(!r || !validPath(r.path) || declared[r.path]) bad('Invalid/duplicate manifest path');
        if(r.type==='symlink') {
            if(typeof r.target!=='string' || !r.target || r.target[0]==='/' || /[\\\x00-\x1f\x7f]/.test(r.target)) bad('Invalid link target');
        } else if(r.type!==undefined || !/^[0-9a-f]{64}$/.test(r.sha256)) bad('Invalid manifest file hash/type');
        declared[r.path]=r;
    });
    if(args[0]==='after') {
        var root=args[1], fm=$.NSFileManager.defaultManager, en=fm.enumeratorAtPath(root), x, seen=Object.create(null), sums=[];
        while((x=en.nextObject)) {
            var rel=str(x), full=root+'/'+rel, attrs=ObjC.deepUnwrap(fm.attributesOfItemAtPathError(full,null));
            if(!attrs || typeof attrs!=='object' || typeof attrs.NSFileType!=='string') bad('Cannot read extracted file attributes: '+rel);
            var type=attrs.NSFileType;
            if(type==='NSFileTypeDirectory') continue;
            var r=declared[rel]; if(!r || seen[rel]) bad('Undeclared extracted file: '+rel); seen[rel]=true;
            if(r.type==='symlink') {
                var target=fm.destinationOfSymbolicLinkAtPathError(full,null);
                var targetText=typeof target==='string' ? target : (target ? ObjC.unwrap(target) : null);
                if(type!=='NSFileTypeSymbolicLink' || typeof targetText!=='string' || targetText!==r.target) bad('Extracted symlink mismatch: '+rel);
            } else {
                if(type!=='NSFileTypeRegular') bad('Extracted type mismatch: '+rel);
                sums.push(r.sha256+'  '+full);
            }
        }
        if(Object.keys(seen).length!==m.files.length) bad('Missing extracted files');
        var info=json(root+'/Contents/Resources/build-info.json');
        if(info.version!==u.version || info.source_commit!==u.source_commit || info.source_digest!==u.source_digest || info.build_number!==u.build_number) bad('Bundle build-info mismatch');
        if(dataText($.NSData.dataWithContentsOfFile(root+'/Contents/Resources/VERSION')).trim()!==u.version) bad('Bundle VERSION mismatch');
        return sums.join('\n')+'\n';
    }
    if(args[0]!=='before') bad('Invalid verifier operation');
    var handle=$.NSFileHandle.fileHandleForReadingAtPath(args[1]); if(!handle) bad('Cannot read ZIP');
    var size=Number(handle.seekToEndOfFile);
    function read(off,n) {
        if(off<0 || n<0 || off+n>size) bad('ZIP record out of bounds');
        handle.seekToFileOffset(off); var a=bytes(handle.readDataOfLength(n));
        if(a.length!==n) bad('Truncated ZIP'); return a;
    }
    function u16(a,p) { return a[p]+a[p+1]*256; }
    function u32(a,p) { return u16(a,p)+u16(a,p+2)*65536; }
    function extras(a) {
        for(var p=0;p<a.length;) {
            if(p+4>a.length) bad('Invalid ZIP extra record');
            var tag=u16(a,p), len=u16(a,p+2); p+=4;
            if(p+len>a.length || tag===1 || tag===0x000d || tag===0x7075 || tag===0x6375) bad('Unsupported ZIP64/name override');
            p+=len;
        }
    }
    var tail=read(Math.max(0,size-65557),Math.min(size,65557)), end=-1;
    for(var e=tail.length-22;e>=0;e--) if(u32(tail,e)===0x06054b50 && e+22+u16(tail,e+20)===tail.length) { end=e; break; }
    if(end<0 || u16(tail,end+4)!==0 || u16(tail,end+6)!==0 || u16(tail,end+8)!==u16(tail,end+10)) bad('Unsupported ZIP layout');
    var spellings=Object.create(null);
    function spelling(path) {
        var parts=path.split("/"), acc="";
        parts.forEach(function(part){acc+=(acc?"/":"")+part; var key=acc.normalize("NFD").toUpperCase().toLowerCase().normalize("NFD"); if(spellings[key] && spellings[key]!==acc) bad("Case/Unicode path alias"); spellings[key]=acc;});
    }
    var count=u16(tail,end+10), cdSize=u32(tail,end+12), cdOff=u32(tail,end+16), endOff=size-tail.length+end;
    if(!count || count>40000 || cdSize>32*1024*1024 || cdOff+cdSize!==endOff) bad('Invalid ZIP directory');
    var cd=read(cdOff,cdSize), p=0, nodes=Object.create(null), links=Object.create(null), seen=Object.create(null), spans=[], expanded=0;
    for(var i=0;i<count;i++) {
        if(p+46>cd.length || u32(cd,p)!==0x02014b50) bad('Invalid ZIP central record');
        var host=cd[p+5], flags=u16(cd,p+8), method=u16(cd,p+10), crc=u32(cd,p+16), compressed=u32(cd,p+20), raw=u32(cd,p+24), nl=u16(cd,p+28), el=u16(cd,p+30), cl=u16(cd,p+32), disk=u16(cd,p+34), attrs=u32(cd,p+38), offset=u32(cd,p+42);
        if(p+46+nl+el+cl>cd.length || disk || (flags&~0x080e) || (flags&1) || (method!==0 && method!==8)) bad('Unsupported ZIP entry');
        var name=utf8(cd.slice(p+46,p+46+nl)); extras(cd.slice(p+46+nl,p+46+nl+el));
        p+=46+nl+el+cl;
        var dir=name.slice(-1)==='/', canonical=dir?name.slice(0,-1):name;
        if(!validPath(canonical) || !(canonical==='CodexTokenDesktop.app' || canonical.indexOf('CodexTokenDesktop.app/')===0 || canonical==='__MACOSX/CodexTokenDesktop.app' || canonical.indexOf('__MACOSX/CodexTokenDesktop.app/')===0)) bad('Unsafe ZIP path: '+name);
        spelling(canonical);
        var unix=attrs>>>16, kind=unix&0xf000, link=kind===0xa000;
        if(nodes[canonical] || (kind!==0 && kind!==0x4000 && kind!==0x8000 && kind!==0xa000) || (dir && (link || kind===0x8000)) || (kind===0x4000 && !dir)) bad('Invalid ZIP node type/duplicate');
        if((attrs&0x10) && !dir) bad('Conflicting ZIP directory attribute');
        nodes[canonical]=dir?'directory':(link?'link':'file'); expanded+=raw;
        if(expanded>512*1024*1024) bad('ZIP expands beyond 512 MiB');
        var h=read(offset,30); if(u32(h,0)!==0x04034b50 || u16(h,6)!==flags || u16(h,8)!==method) bad('ZIP local/central mismatch');
        var hn=u16(h,26), he=u16(h,28), extra=read(offset+30,hn+he);
        if(utf8(extra.slice(0,hn))!==name) bad('ZIP local name mismatch'); extras(extra.slice(hn));
        if(!(flags&8) && (u32(h,14)!==crc || u32(h,18)!==compressed || u32(h,22)!==raw)) bad('ZIP local sizes mismatch');
        var finish=offset+30+hn+he+compressed;
        if(flags&8) {
            var d=read(finish,12), shift=u32(d,0)===0x08074b50?4:0;
            if(shift) d=read(finish,16);
            if(u32(d,shift)!==crc || u32(d,shift+4)!==compressed || u32(d,shift+8)!==raw) bad('Invalid ZIP data descriptor');
            finish+=12+shift;
        }
        if(finish>cdOff) bad('ZIP data overlaps directory'); spans.push([offset,finish]);
        if(canonical.indexOf('__MACOSX/')===0) { if(link) bad('Metadata cannot contain links'); continue; }
        if(dir) continue;
        if(canonical==='CodexTokenDesktop.app') bad('Application root is not a directory');
        var rel=canonical.slice('CodexTokenDesktop.app/'.length), r=declared[rel];
        if(!r) bad('Undeclared ZIP file: '+rel); seen[rel]=true;
        if(r.type==='symlink') {
            if(!link || raw>4096) bad('ZIP link type/size mismatch');
            // unzip's filename selector is a pattern: reject pattern characters.
            if(/[\[\]*?]/.test(name)) bad('Unsupported link selector');
            var target=task('/usr/bin/unzip',['-p',args[1],name]);
            if(target!==r.target) bad('ZIP symlink target mismatch'); links[canonical]=target;
        } else if(link) bad('Unexpected ZIP symlink');
    }
    if(p!==cd.length || Object.keys(seen).length!==m.files.length) bad('ZIP/manifest entries mismatch');
    spans.sort(function(a,b){return a[0]-b[0];}); var cursor=0;
    spans.forEach(function(s){if(s[0]!==cursor) bad('ZIP contains overlapping/unlisted local entries');cursor=s[1];});
    if(cursor!==cdOff) bad('ZIP contains unlisted bytes before directory');
    Object.keys(nodes).forEach(function(name){
        var parts=name.split('/'); while(parts.length>1) {parts.pop(); var k=nodes[parts.join('/')]; if(k && k!=='directory') bad('ZIP child below file/link');}
    });
    Object.keys(links).forEach(function(name){
        var todo=name.split('/').slice(0,-1).concat(links[name].split('/')), done=[], expansions=0;
        while(todo.length) {
            var part=todo.shift(); if(part==='' || part==='.') continue;
            if(part==='..') {if(done.length<=1) bad('ZIP link escapes bundle');done.pop();continue;}
            done.push(part); spelling(done.join('/')); if(done[0]!=='CodexTokenDesktop.app') bad('ZIP link escapes bundle');
            var nested=links[done.join('/')]; if(nested!==undefined) {if(++expansions>40) bad('ZIP link cycle');done.pop();todo=nested.split('/').concat(todo);}
        }
    });
    handle.closeFile;
    return 'ZIP paths, local records and symlink chains verified';
}
JXA
fetch() {
    local current=$1 status location attempt
    local cdn_pattern='^https://([A-Za-z0-9-]+\.)*githubusercontent\.com/[^[:space:]\\]*$'
    local release_pattern="^https://github[.]com/$REPO/releases/download/[A-Za-z0-9._~-]+/[A-Za-z0-9._-]+$"
    if [ "$current" != "$LATEST_UPDATE" ]; then
        [[ "$current" =~ $release_pattern ]] && [ -n "$RELEASE_BASE" ] && [ "${current%/*}" = "$RELEASE_BASE" ] || fail '下载地址不属于已固定的 Release'
    fi
    for attempt in 1 2 3 4 5 6; do
        status=$(/usr/bin/curl --fail --silent --show-error --proto '=https' \
            --connect-timeout 30 --max-time 300 --max-filesize "$3" --retry 2 \
            --user-agent CodexTokenDashboard-Installer --dump-header "$STAGE/http.headers" \
            --write-out '%{http_code}' --output "$2" "$current")
        case "$status" in
            200) [ "$(/usr/bin/stat -f %z "$2")" -le "$3" ] || fail '下载超过大小限制'; return 0 ;;
            301|302|303|307|308)
                location=$(/usr/bin/awk 'tolower($1)=="location:" {sub(/^[^:]*:[ \t]*/, ""); sub(/\r$/, ""); value=$0} END {print value}' "$STAGE/http.headers")
                if [[ "$location" =~ $release_pattern ]]; then
                    case "$location" in */./*|*/../*) fail 'Release 重定向路径无效' ;; esac
                    if [ "$current" = "$LATEST_UPDATE" ]; then
                        [ "${location##*/}" = update.json ] || fail 'latest 重定向不是 update.json'
                        RELEASE_BASE=${location%/*}
                    fi
                    [ -n "$RELEASE_BASE" ] && [ "${location%/*}" = "$RELEASE_BASE" ] || fail 'Release 重定向改变了已固定版本'
                elif [[ "$location" =~ $cdn_pattern ]]; then
                    [ -n "$RELEASE_BASE" ] || fail 'latest 未提供可固定的 Release 地址'
                else
                    fail 'GitHub 下载重定向到不受支持的地址'
                fi
                current=$location ;;
            *) fail "GitHub 下载 HTTP 状态异常：$status" ;;
        esac
    done
    fail 'GitHub 下载重定向次数过多'
}
sha() { /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'; }
printf '%s\n' '正在读取最新 GitHub Release…'
# Call fetch directly so its pinned RELEASE_BASE survives in this shell.
fetch "$LATEST_UPDATE" "$STAGE/update.json" 2097152
[ -n "$RELEASE_BASE" ] || fail '无法确定最新 Release 的固定下载目录'
[ "$(get "$STAGE/update.json" schema)" = 1 ] || fail '不支持的更新协议'
VERSION=$(get "$STAGE/update.json" version)
COMMIT=$(get "$STAGE/update.json" source_commit)
DIGEST=$(get "$STAGE/update.json" source_digest)
ARCHIVE=$(get "$STAGE/update.json" packages.macos-arm64.archive)
MANIFEST=$(get "$STAGE/update.json" packages.macos-arm64.manifest)
ARCHIVE_SHA=$(get "$STAGE/update.json" packages.macos-arm64.sha256)
MANIFEST_SHA=$(get "$STAGE/update.json" packages.macos-arm64.manifest_sha256)
ARCHIVE_SIZE=$(get "$STAGE/update.json" packages.macos-arm64.size)
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail '版本号无效'
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] && [[ "$DIGEST" =~ ^[0-9a-f]{64}$ ]] || fail '源码信息无效'
[[ "$ARCHIVE_SHA" =~ ^[0-9a-f]{64}$ ]] && [[ "$MANIFEST_SHA" =~ ^[0-9a-f]{64}$ ]] || fail '校验摘要无效'
[[ "$ARCHIVE_SIZE" =~ ^[1-9][0-9]{0,8}$ ]] && [ "$ARCHIVE_SIZE" -le 262144000 ] || fail '压缩包大小无效'
[[ "$ARCHIVE" =~ ^[A-Za-z0-9._-]+\.zip$ ]] && [[ "$MANIFEST" =~ ^[A-Za-z0-9._-]+\.json$ ]] || fail '发布附件名称无效'
fetch "$RELEASE_BASE/$MANIFEST" "$STAGE/manifest.json" 2097152
[ "$(sha "$STAGE/manifest.json")" = "$MANIFEST_SHA" ] || fail 'Manifest SHA-256 不一致'
printf '正在下载并验证 %s…\n' "$VERSION"
fetch "$RELEASE_BASE/$ARCHIVE" "$STAGE/archive.zip" 262144000
[ "$(/usr/bin/stat -f %z "$STAGE/archive.zip")" = "$ARCHIVE_SIZE" ] || fail 'ZIP 大小不一致'
[ "$(sha "$STAGE/archive.zip")" = "$ARCHIVE_SHA" ] || fail 'ZIP SHA-256 不一致'
/usr/bin/osascript -l JavaScript "$STAGE/verify.js" before "$STAGE/archive.zip" "$STAGE/manifest.json" "$STAGE/update.json" >/dev/null
/bin/mkdir "$STAGE/candidate"
/usr/bin/ditto -x -k "$STAGE/archive.zip" "$STAGE/candidate"
CANDIDATE="$STAGE/candidate/CodexTokenDesktop.app"
identity "$CANDIDATE" || fail '候选应用身份不一致'
/usr/bin/osascript -l JavaScript "$STAGE/verify.js" after "$CANDIDATE" "$STAGE/manifest.json" "$STAGE/update.json" > "$STAGE/checksums"
/usr/bin/shasum -a 256 --check --status "$STAGE/checksums" || fail '应用文件 SHA-256 不一致'
/usr/bin/codesign --verify --deep --strict "$CANDIDATE"
[ "$(/usr/bin/lipo -archs "$CANDIDATE/Contents/MacOS/CodexTokenDesktop")" = arm64 ] || fail '候选程序不是 arm64'
if [ "$CHECK" -eq 1 ]; then printf '校验通过：%s（%s）。未替换应用。\n' "$VERSION" "$COMMIT"; exit 0; fi
validate_path "$TARGET"
[ "$(cd "$PARENT" && /bin/pwd -P)" = "$PARENT" ] || fail '安装路径发生变化'
not_running
check_update_lock
if [ -e "$TARGET" ]; then
    identity "$TARGET" || fail '安装目标发生变化'
    BACKUP="$TARGET.rollback-${STAGE##*.}"
    [ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ] || fail '备份路径已存在'
    /bin/mv "$TARGET" "$BACKUP"
fi
CANDIDATE_ID=$(/usr/bin/stat -f '%d:%i' "$CANDIDATE")
/bin/mv "$CANDIDATE" "$TARGET"
MOVED=1
/usr/bin/codesign --verify --deep --strict "$TARGET"
COMMITTED=1
printf '已安装 %s：%s\n' "$VERSION" "$TARGET"
[ -z "$BACKUP" ] || printf '旧应用备份保留：%s\n' "$BACKUP"
if [ "$LAUNCH" -eq 1 ]; then
    /usr/bin/open "$TARGET" || { printf '应用已安装，但系统未允许启动。请从 Finder 打开：%s\n' "$TARGET" >&2; exit 1; }
fi
