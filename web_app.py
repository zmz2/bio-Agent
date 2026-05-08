"""
FastAPI web app for the Bio Deep Research agent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import html
import shutil
import tempfile
import requests
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import BioResearchAgent
from config import get_config


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="生物信息学 Deep Research Agent", version="2.0.0")


@dataclass
class TaskState:
    task_id: Optional[str] = None
    status: str = "idle"
    current_stage: str = "等待输入"
    progress: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)
    clarification_needed: list[str] = field(default_factory=list)
    rewritten_brief: str = ""
    research_plan: dict[str, Any] = field(default_factory=dict)
    active_source: str = ""
    round_summary: dict[str, Any] = field(default_factory=dict)
    evidence_cards: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    final_conclusion: str = ""
    stop_reason: str = ""
    error: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


task_state = TaskState()


class ResearchRequest(BaseModel):
    input_text: str = ""
    folder_path: str = ""


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    with open("templates/index.html", "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


def _normalize_upload_filename(filename: str) -> str:
    raw = (filename or "").replace("\\", "/").strip()
    parts = [part for part in raw.split("/") if part not in {"", ".", ".."} and not part.endswith(":")]
    if not parts:
        raise HTTPException(status_code=400, detail="上传文件名无效")
    return "/".join(parts)


def _derive_folder_label(files: list[UploadFile], explicit_label: str) -> str:
    if explicit_label.strip():
        return explicit_label.strip()
    for upload in files:
        if not upload.filename:
            continue
        normalized = _normalize_upload_filename(upload.filename)
        return normalized.split("/", 1)[0]
    return "已选择文件夹"


async def _persist_uploaded_folder(files: list[UploadFile]) -> tuple[Path, int]:
    temp_root = Path(tempfile.mkdtemp(prefix="bio_agent_upload_")).resolve()
    saved_count = 0
    try:
        for upload in files:
            if not upload.filename:
                continue
            relative_name = _normalize_upload_filename(upload.filename)
            target_path = (temp_root / relative_name).resolve()
            if temp_root not in target_path.parents:
                raise HTTPException(status_code=400, detail="上传路径非法")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    handle.write(chunk)
            saved_count += 1

        if saved_count == 0:
            raise HTTPException(status_code=400, detail="请选择至少一个文件")
        return temp_root, saved_count
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    finally:
        for upload in files:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                logger.debug("Failed to close uploaded file handle", exc_info=True)


async def reset_task_state(input_content: str) -> None:
    async with task_state.lock:
        task_state.task_id = datetime.now().strftime("%Y%m%d%H%M%S")
        task_state.status = "running"
        task_state.current_stage = "初始化"
        task_state.progress = 0.0
        task_state.messages = [
            {
                "role": "user",
                "content": input_content,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }
        ]
        task_state.clarification_needed = []
        task_state.rewritten_brief = ""
        task_state.research_plan = {}
        task_state.active_source = ""
        task_state.round_summary = {}
        task_state.evidence_cards = []
        task_state.citations = []
        task_state.final_conclusion = ""
        task_state.stop_reason = ""
        task_state.error = None


async def apply_agent_event(event: dict[str, Any]) -> None:
    async with task_state.lock:
        if "current_stage" in event:
            task_state.current_stage = event["current_stage"]
        if "progress" in event:
            task_state.progress = event["progress"]
        if "clarification_needed" in event:
            task_state.clarification_needed = event["clarification_needed"] or []
        if "rewritten_brief" in event:
            task_state.rewritten_brief = event["rewritten_brief"] or ""
        if "research_plan" in event:
            task_state.research_plan = event["research_plan"] or {}
        if "active_source" in event:
            task_state.active_source = event["active_source"] or ""
        if "round_summary" in event:
            task_state.round_summary = event["round_summary"] or {}
        if "citations" in event:
            task_state.citations = event["citations"] or []
        if "stop_reason" in event:
            task_state.stop_reason = event["stop_reason"] or ""
        if "message" in event and event["message"]:
            task_state.messages.append(
                {
                    "role": "system",
                    "content": event["message"],
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }
            )
        if "evidence_cards" in event:
            existing_ids = {item["id"] for item in task_state.evidence_cards if item.get("id")}
            for card in event["evidence_cards"] or []:
                if card.get("id") in existing_ids:
                    continue
                task_state.evidence_cards.append(card)
            task_state.evidence_cards = task_state.evidence_cards[-20:]


@app.post("/api/research")
async def start_research(request: ResearchRequest) -> dict[str, str]:
    input_content = request.folder_path or request.input_text
    if not input_content.strip():
        raise HTTPException(status_code=400, detail="请输入问题或文件夹路径")

    async with task_state.lock:
        if task_state.status == "running":
            raise HTTPException(status_code=409, detail="已有任务正在运行")

    await reset_task_state(input_content)
    asyncio.create_task(run_research(request.input_text, request.folder_path))
    return {"task_id": task_state.task_id or "", "status": "started"}


@app.post("/api/research/upload-folder")
async def start_uploaded_folder_research(
    files: list[UploadFile] = File(...),
    folder_label: str = Form(""),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="请选择文件夹")

    async with task_state.lock:
        if task_state.status == "running":
            raise HTTPException(status_code=409, detail="已有任务正在运行")

    display_label = _derive_folder_label(files, folder_label)
    temp_root, saved_count = await _persist_uploaded_folder(files)
    await reset_task_state(f"文件夹研究：{display_label}")
    asyncio.create_task(
        run_research(
            folder_path=str(temp_root),
            folder_display_name=display_label,
            cleanup_path=str(temp_root),
        )
    )
    return {
        "task_id": task_state.task_id or "",
        "status": "started",
        "folder_label": display_label,
        "file_count": saved_count,
    }


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    async with task_state.lock:
        return {
            "status": task_state.status,
            "current_stage": task_state.current_stage,
            "progress": task_state.progress,
            "messages": task_state.messages,
            "clarification_needed": task_state.clarification_needed,
            "rewritten_brief": task_state.rewritten_brief,
            "research_plan": task_state.research_plan,
            "active_source": task_state.active_source,
            "round_summary": task_state.round_summary,
            "evidence_cards": task_state.evidence_cards,
            "search_results": task_state.evidence_cards,
            "citations": task_state.citations,
            "final_conclusion": task_state.final_conclusion,
            "stop_reason": task_state.stop_reason,
            "error": task_state.error,
        }


@app.get("/api/stream")
async def stream_status() -> StreamingResponse:
    async def event_generator() -> Any:
        while True:
            async with task_state.lock:
                payload = {
                    "status": task_state.status,
                    "current_stage": task_state.current_stage,
                    "progress": task_state.progress,
                    "messages": task_state.messages,
                    "clarification_needed": task_state.clarification_needed,
                    "rewritten_brief": task_state.rewritten_brief,
                    "research_plan": task_state.research_plan,
                    "active_source": task_state.active_source,
                    "round_summary": task_state.round_summary,
                    "evidence_cards": task_state.evidence_cards,
                    "search_results": task_state.evidence_cards,
                    "citations": task_state.citations,
                    "final_conclusion": task_state.final_conclusion,
                    "stop_reason": task_state.stop_reason,
                    "error": task_state.error,
                }

            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if payload["status"] in {"completed", "failed"}:
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/export")
async def export_results() -> dict[str, Any]:
    """导出研究结果到文件夹，包含报告Markdown文件和完整参考文献文件夹。"""
    import os
    
    async with task_state.lock:
        if task_state.status != "completed" or not task_state.final_conclusion:
            raise HTTPException(status_code=400, detail="暂无可导出的结果")
        
        conclusion = task_state.final_conclusion
        citations = task_state.citations
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # 创建导出文件夹
        export_dir = Path(f"./exports/{current_time}_研究结果").resolve()
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建引用文件夹
        citations_dir = export_dir / "引用文献"
        citations_dir.mkdir(parents=True, exist_ok=True)
        
        # 保存研究报告（Markdown格式）
        report_content = f"""# 深度研究报告

生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

---

{conclusion}
"""
        report_file = export_dir / "研究报告.md"
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report_content)
        
        # 保存参考文献索引（包含链接）
        references_content = "# 参考文献索引\n\n"
        references_content += f"共 {len(citations)} 篇参考文献，详细内容请查看 `引用文献/` 文件夹。\n\n"
        references_content += "---\n\n"
        
        for idx, citation in enumerate(citations, 1):
            label = citation.get("label", f"C{idx}")
            title = citation.get("title", "N/A")
            url = citation.get("url", "")
            source = citation.get("source", "unknown")
            source_type = citation.get("source_type", "unknown")
            
            references_content += f"## [{label}] {title}\n"
            references_content += f"- **来源**: {source} ({source_type})\n"
            references_content += f"- **链接**: {url}\n"
            references_content += f"- **详情**: [引用文献/{label}.md](引用文献/{label}.md)\n\n"
        
        references_file = export_dir / "参考文献索引.md"
        with open(references_file, "w", encoding="utf-8") as f:
            f.write(references_content)
        
        # 为每个引用创建单独文件
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        })
        
        for idx, citation in enumerate(citations, 1):
            label = citation.get("label", f"C{idx}")
            title = citation.get("title", "N/A")
            url = citation.get("url", "")
            source = citation.get("source", "unknown")
            source_type = citation.get("source_type", "unknown")
            claim = citation.get("claim_summary", "")
            year = citation.get("year", "")
            full_content = citation.get("full_content", "")
            
            # 创建引用文件内容
            citation_content = f"# [{label}] {title}\n\n"
            citation_content += f"## 基本信息\n\n"
            citation_content += f"- **标签**: {label}\n"
            citation_content += f"- **来源**: {source}\n"
            citation_content += f"- **类型**: {source_type}\n"
            citation_content += f"- **年份**: {year}\n"
            citation_content += f"- **链接**: {url}\n\n"
            
            if claim:
                citation_content += f"## 引用要点\n\n{claim}\n\n"
            
            citation_content += f"---\n\n"
            
            # 优先使用已获取的完整原文内容
            if full_content and len(full_content) > 100:
                citation_content += f"## 全文内容\n\n{full_content}\n"
            else:
                # 如果没有全文内容，尝试重新获取
                detailed_content = ""
                if source_type == "pubmed":
                    detailed_content = _fetch_pubmed_abstract(url, session)
                elif source_type == "web":
                    detailed_content = _fetch_web_content(url, session)
                elif source_type == "clinvar":
                    detailed_content = _fetch_clinvar_details(url, session)
                elif source_type == "uniprot":
                    detailed_content = _fetch_uniprot_details(url, session)
                
                if detailed_content:
                    citation_content += f"## 全文内容\n\n{detailed_content}\n"
                else:
                    citation_content += f"## 全文内容\n\n*无法获取全文内容，请访问原文链接查看。*\n"
            
            # 保存引用文件
            citation_file = citations_dir / f"{label}.md"
            with open(citation_file, "w", encoding="utf-8") as f:
                f.write(citation_content)
        
        return {
            "success": True,
            "export_path": str(export_dir),
            "report_file": str(report_file),
            "references_file": str(references_file),
            "citations_dir": str(citations_dir),
            "citations_count": len(citations),
            "message": f"结果已导出至: {export_dir}，包含 {len(citations)} 篇参考文献的详细内容"
        }


def _fetch_pubmed_abstract(url: str, session: requests.Session) -> str:
    """获取PubMed摘要."""
    import xml.etree.ElementTree as ET
    
    # 从URL提取PMID
    pmid_match = re.search(r'/(\d+)/?$', url) or re.search(r'pubmed/(\d+)', url)
    if not pmid_match:
        return "*无法提取PMID*"
    
    pmid = pmid_match.group(1)
    
    try:
        # 获取摘要
        response = session.get(
            f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={
                "db": "pubmed",
                "id": pmid,
                "retmode": "xml",
                "rettype": "abstract",
            },
            timeout=15
        )
        
        if response.status_code != 200:
            return "*获取摘要失败*"
        
        # 解析XML提取摘要
        root = ET.fromstring(response.text)
        
        abstract_parts = []
        
        # 提取文章标题
        article_title = ""
        title_elem = root.find(".//ArticleTitle")
        if title_elem is not None:
            article_title = "".join(title_elem.itertext()).strip()
        
        # 提取作者
        authors = []
        for author in root.findall(".//AuthorList/Author"):
            last_name = author.findtext("LastName", "")
            fore_name = author.findtext("ForeName", "")
            if last_name:
                authors.append(f"{last_name} {fore_name}".strip())
        
        # 提取摘要文本
        abstract_elem = root.find(".//Abstract")
        if abstract_elem is not None:
            for abstract_text in abstract_elem.findall(".//AbstractText"):
                label = abstract_text.get("Label", "")
                text = abstract_text.text or ""
                if label:
                    abstract_parts.append(f"**{label}:** {text}")
                else:
                    abstract_parts.append(text)
        
        result = ""
        if article_title:
            result += f"**标题**: {article_title}\n\n"
        if authors:
            result += f"**作者**: {', '.join(authors[:5])}\n\n"
        
        if abstract_parts:
            result += f"**摘要**:\n\n{' '.join(abstract_parts)}"
        else:
            result += "*该文献未提供摘要*"
        
        return result
        
    except Exception as exc:
        return f"*获取摘要失败: {exc}*"


def _fetch_web_content(url: str, session: requests.Session) -> str:
    """获取网页全文内容."""
    try:
        response = session.get(url, timeout=20)
        if response.status_code != 200:
            return f"*网页获取失败 (HTTP {response.status_code})*"
        
        # 解析HTML提取正文
        html_content = response.text
        
        # 移除script和style
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<nav[^>]*>.*?</nav>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<header[^>]*>.*?</header>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<footer[^>]*>.*?</footer>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        
        # 提取正文
        body_match = re.search(r'<body[^>]*>(.*?)</body>', html_content, re.DOTALL | re.IGNORECASE)
        if body_match:
            html_content = body_match.group(1)
        
        # 提取段落
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_content, re.DOTALL | re.IGNORECASE)
        if paragraphs:
            text_content = "\n\n".join(
                re.sub(r'<[^>]+>', '', p).strip()
                for p in paragraphs
            )
        else:
            # 如果没有段落标签，移除所有HTML标签
            text_content = re.sub(r'<[^>]+>', ' ', html_content)
            text_content = html.unescape(text_content)
            text_content = re.sub(r'\s+', ' ', text_content).strip()
        
        # NO TRUNCATION - return complete content
        if text_content:
            return text_content
        else:
            return "*无法提取网页正文*"
            
    except Exception as exc:
        return f"*获取网页内容失败: {exc}*"


def _fetch_clinvar_details(url: str, session: requests.Session) -> str:
    """获取ClinVar详情."""
    try:
        response = session.get(url, timeout=15)
        if response.status_code != 200:
            return "*获取ClinVar详情失败*"
        
        # 提取页面标题和关键信息
        title_match = re.search(r'<title>(.*?)</title>', response.text, re.IGNORECASE)
        title = title_match.group(1) if title_match else "N/A"
        
        # 提取主要描述
        description_match = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', response.text, re.IGNORECASE)
        description = description_match.group(1) if description_match else ""
        
        result = f"**ClinVar记录**: {title}\n\n"
        if description:
            result += f"**描述**: {description}"
        else:
            result += "*请访问链接查看详情*"
        
        return result
        
    except Exception as exc:
        return f"*获取ClinVar详情失败: {exc}*"


def _fetch_uniprot_details(url: str, session: requests.Session) -> str:
    """获取UniProt详情."""
    try:
        response = session.get(url, timeout=15)
        if response.status_code != 200:
            return "*获取UniProt详情失败*"
        
        # 提取页面标题
        title_match = re.search(r'<title>(.*?)</title>', response.text, re.IGNORECASE)
        title = title_match.group(1) if title_match else "N/A"
        
        # 提取描述
        description_match = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', response.text, re.IGNORECASE)
        description = description_match.group(1) if description_match else ""
        
        result = f"**UniProt记录**: {title}\n\n"
        if description:
            result += f"**描述**: {description}"
        else:
            result += "*请访问链接查看详情*"
        
        return result
        
    except Exception as exc:
        return f"*获取UniProt详情失败: {exc}*"


async def run_research(
    input_text: str = "",
    folder_path: str = "",
    folder_display_name: str = "",
    cleanup_path: str = "",
) -> None:
    try:
        config = get_config()
        agent = BioResearchAgent(config)
        loop = asyncio.get_running_loop()

        def on_event(event: dict[str, Any]) -> None:
            asyncio.run_coroutine_threadsafe(apply_agent_event(event), loop)

        if folder_path:
            result = await asyncio.to_thread(agent.run_folder, folder_path, on_event, folder_display_name or None)
        else:
            result = await asyncio.to_thread(agent.run, input_text, on_event)

        async with task_state.lock:
            task_state.status = "completed"
            task_state.progress = 1.0
            task_state.current_stage = "研究完成"
            task_state.final_conclusion = result
            task_state.stop_reason = agent.state.stop_reason if agent.state else task_state.stop_reason
            task_state.citations = agent.state.citations if agent.state else task_state.citations
            task_state.messages.append(
                {
                    "role": "system",
                    "content": "Deep Research 已完成。",
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Web research task failed")
        async with task_state.lock:
            task_state.status = "failed"
            task_state.progress = 1.0
            task_state.current_stage = "研究失败"
            task_state.error = str(exc)
            task_state.stop_reason = str(exc)
            task_state.messages.append(
                {
                    "role": "system",
                    "content": f"研究失败：{exc}",
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }
            )
    finally:
        if cleanup_path:
            shutil.rmtree(cleanup_path, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9000)
