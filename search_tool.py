"""
Search adapters for authority-first bioinformatics research.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

try:
    from tavily import TavilyClient

    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False

try:
    from ddgs import DDGS

    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

from config import Config, get_config


logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Normalized search result across providers."""

    source: str
    source_type: str
    source_id: str
    title: str
    snippet: str
    url: str
    year: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseSearchClient:
    """Base class for search clients."""

    source_type = "base"
    source_name = "Base"
    official = False

    def __init__(self, config: Config, session: Optional[requests.Session] = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", self.config.search.user_agent)

    def search(self, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        raise NotImplementedError

    def _request(self, url: str, *, params: Optional[dict[str, Any]] = None, headers: Optional[dict[str, str]] = None) -> requests.Response:
        """HTTP GET with retry handling."""

        last_error: Exception | None = None
        for attempt in range(self.config.search.max_retries):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.config.ncbi.request_timeout,
                )
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                backoff = self.config.search.retry_delay * (attempt + 1)
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(
                    "%s request failed (%s/%s): %s",
                    self.source_name,
                    attempt + 1,
                    self.config.search.max_retries,
                    exc,
                )
                if attempt < self.config.search.max_retries - 1:
                    if status_code == 429:
                        time.sleep(max(backoff * 2.5, 3.0))
                    else:
                        time.sleep(backoff)

        raise RuntimeError(f"{self.source_name} request failed: {last_error}")


class NCBIClientMixin:
    """Shared helpers for NCBI-backed clients."""

    NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._rate_lock = threading.Lock()
        self._last_request_ts = 0.0

    def _ncbi_params(self, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = {
            "tool": self.config.ncbi.tool,
            "email": self.config.ncbi.email,
        }
        if self.config.ncbi.api_key:
            params["api_key"] = self.config.ncbi.api_key
        if extra:
            params.update(extra)
        return params

    def _respect_rate_limit(self) -> None:
        """Respect NCBI rate limits."""

        interval = 0.11 if self.config.ncbi.api_key else 0.55
        with self._rate_lock:
            now = time.time()
            wait_for = interval - (now - self._last_request_ts)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_request_ts = time.time()

    def _request_ncbi(self, endpoint: str, *, params: Optional[dict[str, Any]] = None) -> requests.Response:
        self._respect_rate_limit()
        merged = self._ncbi_params(params)
        return self._request(f"{self.NCBI_BASE}/{endpoint}", params=merged)


class PubMedClient(NCBIClientMixin, BaseSearchClient):
    """PubMed via NCBI E-utilities."""

    source_type = "pubmed"
    source_name = "PubMed"
    official = True

    def search(self, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        limit = max_results or self.config.search.max_results_per_source

        search_response = self._request_ncbi(
            "esearch.fcgi",
            params={
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": min(limit, 100),
                "sort": "relevance",
            },
        )
        search_data = search_response.json()
        pmids = search_data.get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []

        summary_response = self._request_ncbi(
            "esummary.fcgi",
            params={"db": "pubmed", "id": ",".join(pmids[:limit]), "retmode": "json"},
        )
        summary_data = summary_response.json().get("result", {})
        abstracts = self._fetch_abstracts(pmids[: min(limit, self.config.ncbi.max_abstracts)])

        results: list[SearchResult] = []
        for pmid in pmids[:limit]:
            article = summary_data.get(pmid, {})
            title = article.get("title", "Untitled PubMed record")
            journal = article.get("source", "")
            pubdate = article.get("pubdate", "")
            year = self._extract_year(pubdate)
            authors = ", ".join(author.get("name", "") for author in article.get("authors", [])[:3])
            abstract = abstracts.get(pmid, "")
            snippet_parts = [part for part in [abstract, authors, journal, pubdate] if part]
            snippet = " ".join(snippet_parts).strip() or "PubMed record without abstract."

            results.append(
                SearchResult(
                    source=self.source_name,
                    source_type=self.source_type,
                    source_id=f"PMID:{pmid}",
                    title=title,
                    snippet=snippet,
                    url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    year=year,
                    metadata={
                        "pmid": pmid,
                        "journal": journal,
                        "pubdate": pubdate,
                        "authors": article.get("authors", []),
                    },
                )
            )

        return results

    def _fetch_abstracts(self, pmids: list[str]) -> dict[str, str]:
        if not pmids:
            return {}

        response = self._request_ncbi(
            "efetch.fcgi",
            params={
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml",
                "rettype": "abstract",
            },
        )

        root = ET.fromstring(response.content)
        abstracts: dict[str, str] = {}
        for article in root.findall(".//PubmedArticle"):
            pmid_node = article.find(".//PMID")
            if pmid_node is None or not pmid_node.text:
                continue
            abstract_nodes = article.findall(".//AbstractText")
            abstract_text = " ".join("".join(node.itertext()).strip() for node in abstract_nodes if "".join(node.itertext()).strip())
            abstracts[pmid_node.text] = abstract_text
        return abstracts

    @staticmethod
    def _extract_year(text: str) -> str:
        if not text:
            return ""
        for token in text.split():
            if token[:4].isdigit():
                return token[:4]
        return ""


class ClinVarClient(NCBIClientMixin, BaseSearchClient):
    """ClinVar via NCBI E-utilities."""

    source_type = "clinvar"
    source_name = "ClinVar"
    official = True

    def search(self, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        limit = max_results or self.config.search.max_results_per_source

        search_response = self._request_ncbi(
            "esearch.fcgi",
            params={"db": "clinvar", "term": query, "retmode": "json", "retmax": min(limit, 25)},
        )
        search_data = search_response.json()
        uids = search_data.get("esearchresult", {}).get("idlist", [])
        if not uids:
            return []

        summary_response = self._request_ncbi(
            "esummary.fcgi",
            params={"db": "clinvar", "id": ",".join(uids[:limit]), "retmode": "json"},
        )
        summary_data = summary_response.json().get("result", {})
        linked_pmids = self._fetch_linked_pmids(uids[:limit])

        results: list[SearchResult] = []
        for uid in uids[:limit]:
            record = summary_data.get(uid, {})
            accession = record.get("accession", "")
            title = record.get("title", "Untitled ClinVar record")
            germline = record.get("germline_classification") or {}
            classification = germline.get("description", "")
            review_status = germline.get("review_status", "")
            traits = germline.get("trait_set", []) or []
            trait_names = ", ".join(item.get("trait_name", "") for item in traits if item.get("trait_name"))
            aliases = []
            variation_set = record.get("variation_set", []) or []
            if variation_set:
                aliases = variation_set[0].get("aliases", []) or []
            aliases_text = ", ".join(aliases[:4])
            pmids = linked_pmids.get(uid, [])

            parts = []
            if classification:
                parts.append(f"germline classification: {classification}")
            if review_status:
                parts.append(f"review status: {review_status}")
            if trait_names:
                parts.append(f"traits: {trait_names}")
            if aliases_text:
                parts.append(f"aliases: {aliases_text}")
            if pmids:
                parts.append(f"linked PMIDs: {', '.join(pmids[:5])}")

            results.append(
                SearchResult(
                    source=self.source_name,
                    source_type=self.source_type,
                    source_id=accession or f"ClinVar:{uid}",
                    title=title,
                    snippet="; ".join(parts) or "ClinVar record",
                    url=f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{uid}/",
                    metadata={
                        "uid": uid,
                        "accession": accession,
                        "classification": classification,
                        "review_status": review_status,
                        "trait_names": trait_names,
                        "aliases": aliases,
                        "linked_pmids": pmids,
                    },
                )
            )

        return results

    def _fetch_linked_pmids(self, clinvar_ids: list[str]) -> dict[str, list[str]]:
        if not clinvar_ids:
            return {}

        response = self._request_ncbi(
            "elink.fcgi",
            params={"dbfrom": "clinvar", "db": "pubmed", "id": ",".join(clinvar_ids), "retmode": "json"},
        )
        data = response.json()
        mapping: dict[str, list[str]] = {}

        for linkset in data.get("linksets", []):
            source_ids = linkset.get("ids", [])
            if not source_ids:
                continue
            clinvar_id = source_ids[0]
            pmids: list[str] = []
            for linksetdb in linkset.get("linksetdbs", []):
                if linksetdb.get("dbto") != "pubmed":
                    continue
                pmids.extend(linksetdb.get("links", []))
            mapping[clinvar_id] = pmids
        return mapping


class UniProtClient(BaseSearchClient):
    """UniProt REST API client."""

    source_type = "uniprot"
    source_name = "UniProt"
    official = True

    API_URL = "https://rest.uniprot.org/uniprotkb/search"

    def search(self, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        limit = max_results or self.config.search.max_results_per_source
        normalized_query = query if "reviewed:" in query else f"{query} AND reviewed:true"
        response = self._request(
            self.API_URL,
            params={
                "query": normalized_query,
                "format": "json",
                "size": limit,
                "fields": "accession,id,protein_name,gene_names,organism_name,cc_function,ft_domain",
            },
            headers={"Accept": "application/json"},
        )
        data = response.json()
        results: list[SearchResult] = []
        for item in data.get("results", []):
            accession = item.get("primaryAccession", "")
            protein_name = (
                item.get("proteinDescription", {})
                .get("recommendedName", {})
                .get("fullName", {})
                .get("value", "")
            )
            gene_name = ""
            genes = item.get("genes", []) or []
            if genes:
                gene_name = genes[0].get("geneName", {}).get("value", "")
            organism = item.get("organism", {}).get("scientificName", "")
            function_text = self._extract_function_comment(item.get("comments", []))
            domains = self._extract_domains(item.get("features", []))

            snippet_parts = [part for part in [function_text, domains, organism] if part]
            snippet = " ".join(snippet_parts).strip() or protein_name or accession

            results.append(
                SearchResult(
                    source=self.source_name,
                    source_type=self.source_type,
                    source_id=accession,
                    title=f"{gene_name or accession} - {protein_name}".strip(" -"),
                    snippet=snippet,
                    url=f"https://www.uniprot.org/uniprotkb/{accession}",
                    metadata={"accession": accession, "organism": organism, "gene": gene_name},
                )
            )

        return results

    @staticmethod
    def _extract_function_comment(comments: list[dict[str, Any]]) -> str:
        for comment in comments:
            if comment.get("commentType") != "FUNCTION":
                continue
            texts = comment.get("texts", []) or []
            if texts:
                return texts[0].get("value", "")
        return ""

    @staticmethod
    def _extract_domains(features: list[dict[str, Any]]) -> str:
        domains = []
        for feature in features:
            if feature.get("type") != "Domain":
                continue
            description = feature.get("description", "")
            start = feature.get("location", {}).get("start", {}).get("value")
            end = feature.get("location", {}).get("end", {}).get("value")
            if description and start and end:
                domains.append(f"{description} domain ({start}-{end})")
        if not domains:
            return ""
        return "Domains: " + ", ".join(domains[:4])


class WebContentFetcher:
    """抓取网页全文内容的工具."""
    
    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
    
    def fetch_content(self, url: str, max_chars: int = 8000) -> str:
        """抓取网页全文内容，提取正文部分."""
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            # 解析HTML提取正文
            content = self._extract_text_from_html(response.text, max_chars)
            return content
        except Exception as exc:
            logger.warning("Failed to fetch content from %s: %s", url, exc)
            return f"（无法获取该网页的全文内容：{exc}）"
    
    def _extract_text_from_html(self, html: str, max_chars: int) -> str:
        """从HTML中提取纯文本."""
        import re
        from html import unescape
        
        # 移除script和style标签
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        
        # 移除HTML标签
        text = re.sub(r'<[^>]+>', ' ', html)
        
        # 解码HTML实体
        text = unescape(text)
        
        # 清理空白
        text = re.sub(r'\s+', ' ', text).strip()
        
        # 截断
        if len(text) > max_chars:
            text = text[:max_chars] + "..."
        
        return text
    
    def fetch_multiple(self, urls: list[str], max_per_page: int = 8000) -> dict[str, str]:
        """批量抓取多个网页."""
        results = {}
        for url in urls:
            if url:
                results[url] = self.fetch_content(url, max_per_page)
        return results


class WebFallbackClient(BaseSearchClient):
    """Wide web fallback using Tavily first, DDG second."""

    source_type = "web"
    source_name = "Web"
    official = False

    def __init__(self, config: Config, session: Optional[requests.Session] = None):
        super().__init__(config, session=session)
        self._tavily: Optional[TavilyClient] = None
        if TAVILY_AVAILABLE and self.config.search.tavily_api_key:
            try:
                self._tavily = TavilyClient(api_key=self.config.search.tavily_api_key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to initialize Tavily: %s", exc)
                self._tavily = None

    def search(self, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        limit = max_results or self.config.search.max_results_per_source
        if self._tavily is not None:
            return self._search_tavily(query, limit)
        return self._search_ddg(query, limit)

    def _search_tavily(self, query: str, max_results: int) -> list[SearchResult]:
        response = self._tavily.search(
            query=query,
            max_results=max_results,
            include_answer=False,
            include_raw_content=False,
        )
        results: list[SearchResult] = []
        for item in response.get("results", []):
            url = item.get("url", "")
            results.append(
                SearchResult(
                    source=self._extract_source_name(url),
                    source_type=self.source_type,
                    source_id=url,
                    title=item.get("title", ""),
                    snippet=item.get("content", ""),
                    url=url,
                )
            )
        return results

    def _search_ddg(self, query: str, max_results: int) -> list[SearchResult]:
        if not DDGS_AVAILABLE:
            raise RuntimeError("No web fallback backend available. Install duckduckgo-search or configure Tavily.")

        results: list[SearchResult] = []
        try:
            with DDGS() as ddgs:
                items = list(ddgs.text(query, max_results=max_results))
                for item in items:
                    url = item.get("href", "")
                    if not url:
                        continue
                    results.append(
                        SearchResult(
                            source=self._extract_source_name(url),
                            source_type=self.source_type,
                            source_id=url,
                            title=item.get("title", ""),
                            snippet=item.get("body", ""),
                            url=url,
                        )
                    )
        except Exception as exc:
            logger.warning("DuckDuckGo search failed for query '%s': %s", query, exc)
            # 如果DuckDuckGo失败，尝试使用备用方法
            results = self._search_backup(query, max_results)
        return results
    
    def _search_backup(self, query: str, max_results: int) -> list[SearchResult]:
        """备用搜索方法 - 使用简单的网页抓取."""
        import urllib.parse
        results: list[SearchResult] = []
        
        # 尝试使用几个备用来源
        try:
            # 简单的HTML抓取搜索
            search_urls = [
                f"https://www.google.com/search?q={urllib.parse.quote(query)}",
                f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
            ]
            
            for search_url in search_urls:
                try:
                    response = self.session.get(search_url, timeout=15)
                    if response.status_code == 200:
                        # 简单解析HTML获取链接
                        from html.parser import HTMLParser
                        import re
                        
                        html = response.text
                        # 查找链接
                        links = re.findall(r'href=["\'](https?://[^"\']+)["\']', html)
                        
                        for link in links[:max_results]:
                            if any(domain in link.lower() for domain in ['google', 'duckduck', 'cloudflare']):
                                continue
                            
                            results.append(
                                SearchResult(
                                    source="Web",
                                    source_type="web",
                                    source_id=link,
                                    title=link.split("//")[-1].split("/")[0],
                                    snippet=f"网页内容 - {link}",
                                    url=link,
                                )
                            )
                        
                        if results:
                            break
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Backup search failed: %s", exc)
        
        return results

    @staticmethod
    def _extract_source_name(url: str) -> str:
        if not url:
            return "Web"
        parsed = urlparse(url)
        hostname = parsed.netloc.lower().removeprefix("www.")
        if not hostname:
            return "Web"
        return hostname.split(".")[0].capitalize()


class SearchTool:
    """Authority-first search router."""

    OFFICIAL_SOURCES = {"pubmed", "clinvar", "uniprot"}

    def __init__(self, config: Optional[Config] = None, session: Optional[requests.Session] = None):
        self.config = config or get_config()
        self.session = session or requests.Session()
        self._cache: dict[tuple[str, str, int], list[SearchResult]] = {}
        self.clients = {
            "pubmed": PubMedClient(self.config, session=self.session),
            "clinvar": ClinVarClient(self.config, session=self.session),
            "uniprot": UniProtClient(self.config, session=self.session),
            "web": WebFallbackClient(self.config, session=self.session),
        }

    def search_source(self, source_type: str, query: str, max_results: Optional[int] = None) -> list[SearchResult]:
        if source_type == "web" and not self.config.search.allow_web_fallback:
            return []
        client = self.clients.get(source_type)
        if client is None:
            raise ValueError(f"Unknown search source: {source_type}")
        limit = max_results or self.config.search.max_results_per_source
        cache_key = (source_type, query, limit)
        if cache_key in self._cache:
            return list(self._cache[cache_key])
        results = client.search(query, max_results=limit)
        self._cache[cache_key] = list(results)
        return list(results)

    def search_across_sources(
        self,
        query_by_source: dict[str, str],
        source_priority: list[str],
        max_results: Optional[int] = None,
    ) -> dict[str, list[SearchResult]]:
        results: dict[str, list[SearchResult]] = {}
        for source in source_priority:
            if source == "web" and not self.config.search.allow_web_fallback:
                continue
            query = query_by_source.get(source)
            if not query:
                continue
            source_results = self.search_source(source, query, max_results=max_results)
            results[source] = source_results
        return results


_search_tool: Optional[SearchTool] = None


def get_search_tool() -> SearchTool:
    """Return a cached search tool instance."""

    global _search_tool
    if _search_tool is None:
        _search_tool = SearchTool(get_config())
    return _search_tool


def search(query: str, max_results: int = 5) -> list[SearchResult]:
    """Backward-compatible convenience wrapper using PubMed first."""

    tool = get_search_tool()
    return tool.search_source("pubmed", query, max_results=max_results)
