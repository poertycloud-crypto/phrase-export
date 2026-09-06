#!/usr/bin/env python3
"""Phrase export client, Python 3.9+, standard library only. Never logs secrets."""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

VERSION = "1.0.0"
SKILL = Path(__file__).resolve().parents[1]
PROJECTS = SKILL / "projects.json"
ENDPOINTS = {
    "eu": ("https://eu.phrase.com/idm/oauth/token", "https://cloud.memsource.com/web/api2"),
    "us": ("https://us.phrase.com/idm/oauth/token", "https://us.cloud.memsource.com/web/api2"),
}
STATES = {"NEW", "ACCEPTED", "DECLINED", "REJECTED", "DELIVERED", "EMAILED", "COMPLETED", "CANCELLED"}


class Failure(Exception):
    pass


def config_dir():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "PhraseExport"
    return Path.home() / ".config" / "phrase-export"


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise Failure("无法读取 JSON 配置，请检查文件和 JSON 格式。") from None


def private_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive create: previews and credentials must never silently overwrite.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_credentials(path):
    conf = read_json(path)
    if not isinstance(conf, dict) or set(conf) - {"region", "platform_token"}:
        raise Failure("凭证只接受 region 和 platform_token；不接受任意服务器地址。")
    region, token = conf.get("region"), conf.get("platform_token")
    if region not in ENDPOINTS or not isinstance(token, str) or not token.strip() or "填写" in token:
        raise Failure("请在个人 credentials.json 中填写 region（eu/us）及 Phrase Platform API Token。")
    return region, token.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("服务器要求重定向，已停止；请核对地区配置。")


class Client:
    def __init__(self, region, token):
        self.exchange_url, self.base = ENDPOINTS[region]
        self.token = token
        self.jwt = None
        self.expiry = 0
        self.opener = urllib.request.build_opener(NoRedirect())

    def _send(self, req):
        try:
            with self.opener.open(req, timeout=90) as response:
                # Keep exports bounded rather than consuming unlimited RAM.
                data = response.read(150 * 1024 * 1024 + 1)
                if len(data) > 150 * 1024 * 1024:
                    raise Failure("单次响应超过 150 MB，请缩小导出时间范围。")
                return data
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError):
            raise Failure("网络或 TLS 连接失败；请检查网络/系统证书后重试。未禁用证书验证。") from None

    @staticmethod
    def delay(headers, attempt):
        raw = headers.get("Retry-After", "")
        try:
            seconds = float(raw)
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                seconds = (parsedate_to_datetime(raw) - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                seconds = 2 ** attempt
        if seconds > 60:
            raise Failure("Phrase 要求等待超过 60 秒；请稍后重新尝试。")
        time.sleep(max(0, seconds))

    def authenticate(self):
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": self.token,
        }).encode()
        for attempt in range(4):
            try:
                raw = self._send(urllib.request.Request(self.exchange_url, data=body, headers={
                    "Content-Type": "application/x-www-form-urlencoded"}))
                result = json.loads(raw)
                self.jwt = result["access_token"]
                lifetime = int(result["expires_in"])
                if not isinstance(self.jwt, str) or not self.jwt or lifetime <= 0:
                    raise ValueError()
                self.expiry = time.monotonic() + lifetime
                return
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < 3:
                    self.delay(e.headers, attempt)
                    continue
                raise Failure("Token Exchange 失败（HTTP %s）。请检查 Platform Token、有效期和 eu/us 地区。" % e.code) from None
            except (KeyError, ValueError, TypeError):
                raise Failure("Token Exchange 响应格式异常。") from None

    def api(self, path, params=None, body=None, binary=False):
        # Caller cannot choose arbitrary hosts or mutation paths.
        if not re.fullmatch(r"/v[12]/projects/[A-Za-z0-9]+(?:/jobs(?:/segmentsCount|/bilingualFile)?)?", path):
            raise Failure("不支持的接口路径。")
        if path.split("/")[3] not in {p["uid"] for p in allowed_projects()}:
            raise Failure("项目不在本地白名单中。")
        if body is not None and not path.endswith(("/segmentsCount", "/bilingualFile")):
            raise Failure("只开放查询和导出接口。")
        if self.jwt is None or time.monotonic() >= self.expiry - 60:
            self.authenticate()
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        refreshed = False
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                    headers={"Authorization": "Bearer " + self.jwt, "Content-Type": "application/json", "Accept": "*/*" if binary else "application/json"})
                raw = self._send(req)
                return raw if binary else json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not refreshed and attempt < 4:
                    self.authenticate()
                    refreshed = True
                    continue
                if e.code in (429, 502, 503, 504) and attempt < 4:
                    self.delay(e.headers, attempt)
                    continue
                messages = {401: "鉴权失败", 403: "没有该项目或操作的权限", 404: "项目或资源不存在", 429: "请求频率超过限制"}
                raise Failure("Phrase HTTP %s：%s。" % (e.code, messages.get(e.code, "请求失败"))) from None
            except ValueError:
                raise Failure("Phrase 返回的 JSON 格式异常。") from None

    def jobs(self, uid, level, language):
        found = []
        seen = set()
        for page in range(2000):
            result = self.api("/v2/projects/%s/jobs" % uid, {"pageNumber": page, "pageSize": 50, "workflowLevel": level, "targetLang": language})
            if not isinstance(result.get("content"), list) or not isinstance(result.get("totalPages"), int):
                raise Failure("Job 分页响应缺少 content/totalPages，已停止以避免漏项。")
            for job in result["content"]:
                if job["uid"] in seen:
                    raise Failure("分页出现重复 Job，数据可能正在变化，请重新预览。")
                seen.add(job["uid"])
                found.append(job)
            if page + 1 >= result["totalPages"]:
                return found
        raise Failure("Job 分页超过上限，请联系维护人员。")


def allowed_projects():
    projects = read_json(PROJECTS)["projects"]
    if not projects or any(not re.fullmatch(r"[A-Za-z0-9]+", p["uid"]) for p in projects):
        raise Failure("项目白名单配置无效。")
    return projects


def resolve_project(name):
    matches = [p for p in allowed_projects() if name in (p["uid"], p["name"])]
    if len(matches) != 1:
        raise Failure("项目名需完整且唯一，并在 projects.json 白名单中。先运行 projects 查看。")
    return matches[0]


def timestamp(value):
    try:
        normalized = value.replace("Z", "+00:00")
        normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
        date = dt.datetime.fromisoformat(normalized)
        if date.tzinfo is None:
            raise ValueError()
        return date
    except (ValueError, AttributeError):
        raise Failure("时间必须包含时区，例如 2026-08-01T00:00:00+08:00。") from None


def validate_request(req):
    required = {"project", "language", "date_field", "start", "end", "workflow", "include_cancelled", "output", "completion"}
    optional = {"statuses", "filename_contains"}
    if not isinstance(req, dict) or required - set(req) or set(req) - required - optional:
        raise Failure("需求字段缺失或有未知字段；参照 request.example.json。")
    for key in ("project", "language", "date_field", "start", "workflow", "output", "completion"):
        if not isinstance(req[key], str) or not req[key].strip():
            raise Failure("字段 %s 必须是非空文本。" % key)
    resolve_project(req["project"])
    if req["date_field"] not in ("dateCreated", "dateDue"):
        raise Failure("date_field 只接受 dateCreated 或 dateDue。")
    start = timestamp(req["start"])
    if req["end"] is not None and timestamp(req["end"]) <= start:
        raise Failure("结束时间必须晚于开始时间（起始含边界，结束不含边界）。")
    if type(req["include_cancelled"]) is not bool:
        raise Failure("include_cancelled 必须是 true/false。")
    if req["output"] not in ("segments", "jobs") or req["completion"] not in ("segment", "job"):
        raise Failure("output 只接受 segments/jobs；completion 只接受 segment/job。")
    if req["output"] == "jobs" and req["completion"] != "job":
        raise Failure("Job 清单的完成口径必须是 job。")
    statuses = req.get("statuses", [])
    if not isinstance(statuses, list) or any(s not in STATES for s in statuses):
        raise Failure("statuses 必须是合法状态数组。")
    if "CANCELLED" in statuses and not req["include_cancelled"]:
        raise Failure("statuses 与 include_cancelled 冲突。")
    if not isinstance(req.get("filename_contains", ""), str):
        raise Failure("filename_contains 必须是文本。")
    return req


def selection(client, req):
    project = resolve_project(req["project"])
    detail = client.api("/v1/projects/" + project["uid"])
    if req["language"] not in detail.get("targetLangs", []):
        raise Failure("目标语言不在项目中：" + ", ".join(detail.get("targetLangs", [])))
    steps = detail.get("workflowSteps") or [{"name": "Translation", "workflowLevel": 1}]
    if req["workflow"] == "last":
        step = max(steps, key=lambda s: s["workflowLevel"])
    else:
        matches = [s for s in steps if req["workflow"] in (s["name"], str(s["workflowLevel"]))]
        if len(matches) != 1:
            raise Failure("工作流需明确指定：" + ", ".join(s["name"] for s in steps))
        step = matches[0]
    start, end = timestamp(req["start"]), timestamp(req["end"]) if req["end"] else None
    matches = []
    for job in client.jobs(project["uid"], step["workflowLevel"], req["language"]):
        value = job.get(req["date_field"])
        if value is None:
            if req["date_field"] == "dateDue":
                continue
            raise Failure("Job 缺少创建时间，无法保证筛选完整性。")
        when = timestamp(value)
        if when < start or (end and when >= end):
            continue
        if job.get("targetLang") != req["language"]:
            raise Failure("API 返回了不匹配的目标语言。")
        if job.get("status") == "CANCELLED" and not req["include_cancelled"]:
            continue
        if req.get("statuses") and job.get("status") not in req["statuses"]:
            continue
        if req.get("filename_contains", "") not in job.get("filename", ""):
            continue
        matches.append({k: job.get(k) for k in ("uid", "filename", "status", "dateCreated", "dateDue", "targetLang", "sourceFileUid", "innerId", "split")})
    matches.sort(key=lambda j: (j["dateCreated"], j["filename"], j["uid"]))
    return {"project": project, "workflow": {"name": step["name"], "level": step["workflowLevel"]}, "jobs": matches}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def preview(client, req, folder):
    selected = selection(client, validate_request(req))
    key = secrets.token_hex(12)
    payload = {"request": req, "selection": selected, "created": time.time(), "id": key}
    payload["digest"] = digest(payload)
    private_json(folder / "previews" / (key + ".json"), payload)
    return {"preview_id": key, "expires_minutes": 15, "request": req, **selected,
            "matched_jobs": len(selected["jobs"]), "filename_count": len({j["filename"] for j in selected["jobs"]}),
            "instruction": "请先展示范围和数量，让用户确认后执行 export。"}


def load_preview(folder, key):
    if not re.fullmatch(r"[a-f0-9]{24}", key):
        raise Failure("preview_id 无效。")
    path = folder / "previews" / (key + ".json")
    value = read_json(path)
    checksum = value.pop("digest", None)
    if checksum != digest(value) or value.get("id") != key:
        raise Failure("预览文件被更改，请重新预览。")
    if not 0 <= time.time() - value["created"] <= 900:
        raise Failure("预览已过期，请重新预览并确认。")
    if path.with_suffix(".used").exists():
        raise Failure("该预览已经用于导出，请重新预览。")
    validate_request(value["request"])
    return value, path


def xml_root(raw):
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Failure("首版只接受 UTF-8 XLIFF。") from None
    if "\x00" in decoded:
        raise Failure("XLIFF 编码不受支持。")
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise Failure("XLIFF 包含不支持的 XML 实体声明。")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise Failure("下载结果不是有效 XLIFF。") from None
    if root.tag != "{urn:oasis:names:tc:xliff:document:2.0}xliff":
        raise Failure("首版仅解析 Phrase XLIFF 2.0；不支持的格式已停止。")
    return root


def text_content(elem, data):
    text = elem.text or ""
    for child in elem:
        kind = child.tag.rsplit("}", 1)[-1]
        if kind == "ph":
            ref = child.get("dataRef")
            if ref not in data:
                raise Failure("源文占位符缺少 originalData，已停止以避免丢字。")
            text += data[ref]
        elif kind == "pc":
            if child.get("dataRefStart") not in data or child.get("dataRefEnd") not in data:
                raise Failure("成对标签缺少 originalData。")
            text += data[child.get("dataRefStart")] + text_content(child, data) + data[child.get("dataRefEnd")]
        elif kind == "mrk":
            text += text_content(child, data)
        else:
            raise Failure("未支持的 XLIFF 内联标签：" + kind)
        text += child.tail or ""
    return text


def parse_xliff(root):
    ns = {"x": "urn:oasis:names:tc:xliff:document:2.0"}
    rows = []
    for file in root.findall("x:file", ns):
        for unit in file.findall(".//x:unit", ns):
            data = {x.get("id"): html.unescape(x.text or "") for x in unit.findall("x:originalData/x:data", ns)}
            for seg in unit.findall("x:segment", ns):
                src = seg.find("x:source", ns)
                if src is None:
                    raise Failure("XLIFF 分段缺少 source。")
                state = seg.get("state", "initial")
                if state not in ("initial", "translated", "reviewed", "final"):
                    raise Failure("未知分段状态：" + state)
                substate = next((v for k, v in seg.attrib.items() if k.rsplit("}", 1)[-1] == "subState"), "")
                locked = substate.split(":")[-1] == "locked"
                rows.append([file.get("original", ""), text_content(src, data), state == "final" or locked])
    return rows


def safe_text(value):
    # Text cells are written as inline strings, never as Excel formulas.
    value = "" if value is None else str(value)
    if len(value.encode("utf-16-le")) // 2 > 32767:
        raise Failure("单个单元格超过 Excel 字符上限，请使用 XLIFF。")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)


def csv_literal(value):
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def xlsx(path, headers, rows, widths):
    """Portable data export: a minimal OOXML workbook without third-party installs."""
    if len(rows) > 1048575:
        raise Failure("行数超过 Excel 单表上限，请缩小范围。")
    n = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    def node(parent, tag, attr=None, text=None):
        item = ET.SubElement(parent, tag, attr or {})
        item.text = text
        return item
    root = ET.Element("worksheet", {"xmlns": n})
    views = node(root, "sheetViews")
    view = node(views, "sheetView", {"workbookViewId": "0"})
    node(view, "pane", {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"})
    cols = node(root, "cols")
    for idx, width in enumerate(widths, 1):
        node(cols, "col", {"min": str(idx), "max": str(idx), "width": str(width), "customWidth": "1"})
    data = node(root, "sheetData")
    def colname(index):
        result = ""
        while index:
            index, rem = divmod(index - 1, 26)
            result = chr(65 + rem) + result
        return result
    for i, values in enumerate([headers] + rows, 1):
        row = node(data, "row", {"r": str(i)})
        if i == 1:
            row.set("ht", "28"); row.set("customHeight", "1")
        else:
            lines = max(sum(max(1, (len(line) * 2 + int(width) - 1) // int(width)) for line in str(v or "").split("\n")) for v, width in zip(values, widths))
            row.set("ht", str(min(409, max(20, lines * 15))))
            row.set("customHeight", "1")
        for j, value in enumerate(values, 1):
            cell = node(row, "c", {"r": colname(j) + str(i), "s": "1" if i == 1 else "2"})
            if type(value) is bool:
                cell.set("t", "b"); node(cell, "v", text="1" if value else "0")
            elif isinstance(value, (int, float)):
                node(cell, "v", text=str(value))
            else:
                cell.set("t", "inlineStr")
                node(node(cell, "is"), "t", {"xml:space": "preserve"}, safe_text(value))
    node(root, "autoFilter", {"ref": "A1:%s%d" % (colname(len(headers)), len(rows) + 1)})
    # A fixed format suffices for this tabular exporter; all source content stays literal.
    styles = '''<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="导出数据" sheetId="1" r:id="rId1"/></sheets></workbook>' % n)
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))


def export(client, folder, key, confirmation, output):
    plan, path = load_preview(folder, key)
    if confirmation != key:
        raise Failure("需要用户确认本次预览；确认后使用 --confirm <preview_id>。")
    current = selection(client, plan["request"])
    if current != plan["selection"]:
        raise Failure("Job 清单/状态/工作流在预览后改变，请重新预览和确认。")
    jobs = current["jobs"]
    if not jobs:
        raise Failure("没有匹配 Job，未生成空文件。")
    if len(jobs) > 1000:
        raise Failure("首版单次最多 1000 个 Job，请拆分日期范围；不会静默截断。")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run = output / ("phrase-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + key[:6])
    # Each preview is consumed once, including failed exports; retry needs a new preview.
    private_json(path.with_suffix(".used"), {"started": time.time()})
    run.mkdir(exist_ok=False)
    req = plan["request"]
    manifest = {"version": VERSION, "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(), "request": req, **current}
    if req["output"] == "segments":
        uid = current["project"]["uid"]
        raw = client.api("/v1/projects/" + uid + "/jobs/bilingualFile", {"format": "XLIFF", "preview": "false"}, {"jobs": [{"uid": j["uid"]} for j in jobs]}, binary=True)
        root = xml_root(raw)
        if root.get("trgLang", "").replace("-", "_").lower() != req["language"].replace("-", "_").lower():
            raise Failure("XLIFF 的目标语言与请求不一致。")
        rows = parse_xliff(root)
        if not rows or {r[0] for r in rows} != {j["filename"] for j in jobs}:
            raise Failure("XLIFF 文件范围与 Job 清单不一致，请检查空文件或导出变化。")
        # Segment totals provide an independent check for missed split jobs or state mapping changes.
        totals, completed = 0, 0
        for offset in range(0, len(jobs), 100):
            counts = client.api("/v1/projects/" + uid + "/jobs/segmentsCount", body={"jobs": [{"uid": j["uid"]} for j in jobs[offset:offset+100]]})
            entries = counts["segmentsCountsResults"]
            if {x["jobPartUid"] for x in entries} != {j["uid"] for j in jobs[offset:offset+100]}:
                raise Failure("进度接口缺少 Job，已停止导出。")
            totals += sum(x["counts"]["segmentsCount"] for x in entries)
            completed += sum(x["counts"]["completedSegmentsCount"] for x in entries)
        if totals != len(rows) or (req["completion"] == "segment" and completed != sum(r[2] for r in rows)):
            raise Failure("XLIFF 分段/完成数与实时进度不一致（可能刚有修改），请重新预览后重试。")
        if req["completion"] == "job":
            # Multiple split jobs of the same file may have distinct statuses: fail instead of guessing.
            by_name = collections.defaultdict(set)
            for j in jobs:
                by_name[j["filename"]].add(j["status"])
            if any(len(s) > 1 for s in by_name.values()):
                raise Failure("同名拆分 Job 状态不同，无法按文件映射 Job 完成状态；请选择 segment 口径或导出 jobs。")
            for r in rows:
                r[2] = by_name[r[0]] == {"COMPLETED"}
        headers = ["文件名", "原文", "是否 completed"]
        widths = [55, 100, 20]
        (run / "merged.xliff").write_bytes(raw)
        manifest["counts_api"] = {"segments": totals, "completed_segments": completed}
    else:
        headers = ["项目", "文件名", "目标语言", "工作流", "Job 状态", "是否 completed", "创建时间", "截止时间", "Job UID"]
        rows = [[current["project"]["name"], j["filename"], j["targetLang"], current["workflow"]["name"], j["status"], j["status"] == "COMPLETED", j["dateCreated"], j["dateDue"], j["uid"]] for j in jobs]
        widths = [42, 65, 15, 18, 18, 20, 28, 28, 28]
    xlsx(run / "export.xlsx", headers, rows, widths)
    with (run / "export.csv").open("w", encoding="utf-8-sig", newline="") as f:
        # CSV is safe to open in Excel: formula-leading text receives a literal prefix.
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows([[csv_literal(v) for v in row] for row in rows])
    manifest["rows"] = len(rows)
    manifest["completed_rows"] = sum(row[2] if req["output"] == "segments" else row[5] for row in rows)
    manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.iterdir() if p.is_file()}
    private_json(run / "manifest.json", manifest)
    return {"output_directory": str(run), "matched_jobs": len(jobs), "rows": len(rows), "completed_rows": manifest["completed_rows"], "files": list(manifest["files"])}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Phrase 本地导出；Token 只从个人配置文件读取。")
    p.add_argument("--config", type=Path, default=config_dir() / "credentials.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("auth-check")
    sub.add_parser("config-path")
    sub.add_parser("projects")
    pr = sub.add_parser("preview")
    pr.add_argument("--request", type=Path, required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--preview", required=True)
    ex.add_argument("--confirm", required=True)
    ex.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        if args.command == "config-path":
            emit({"credentials_file": str(args.config.resolve()), "exists": args.config.exists()})
            return
        if args.command == "projects":
            emit({"allowed_projects": allowed_projects()})
            return
        client = Client(*load_credentials(args.config))
        if args.command == "auth-check":
            client.authenticate()
            accessible = []
            for project in allowed_projects():
                try:
                    result = client.api("/v1/projects/" + project["uid"])
                    accessible.append({"name": result["name"], "uid": project["uid"], "languages": result.get("targetLangs", []), "workflow_steps": result.get("workflowSteps", [])})
                except Failure as e:
                    if "HTTP 403" in str(e) or "HTTP 404" in str(e):
                        continue
                    raise
            emit({"authenticated": True, "accessible_allowed_projects": accessible, "jwt_stored": "memory_only"})
        elif args.command == "preview":
            emit(preview(client, read_json(args.request), args.config.parent))
        else:
            emit(export(client, args.config.parent, args.preview, args.confirm, args.output))
    except Failure as e:
        emit({"error": str(e)})
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        # No traceback or raw HTTP response can leak the credential into AI output.
        emit({"error": "本地文件或服务响应异常；未输出凭证。请检查路径/权限或联系维护人员。"})
        sys.exit(1)


if __name__ == "__main__":
    main()
