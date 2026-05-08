"""
Full-text content fetcher for scientific articles and web pages.
Supports PubMed, PubMed Central, Crossref, arXiv, Semantic Scholar, and general web pages.
"""
from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class FullTextFetcher:
    """Fetches full-text content from multiple scientific sources."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        self._cache: dict[str, str] = {}

    def fetch_full_content(self, url: str, source_type: str = "", pmid: str = "", doi: str = "", max_chars: int = 1000000) -> str:
        """Intelligently fetch full-text content based on URL and source type."""
        cache_key = f"{url}|{pmid}|{doi}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        content = ""
        try:
            # Priority 1: Try PMC full-text if PMCID available
            pmcid_match = re.search(r'PMC(\d+)', url, re.IGNORECASE) if url else None
            if pmcid_match:
                pmcid = pmcid_match.group(1)
                content = self._fetch_pmc_full(pmcid)

            # Priority 2: Try PubMed via PMID
            if not content and (pmid or (url and ("pubmed.ncbi.nlm.nih.gov" in url or "pubmed/." in url))):
                if not pmid and url:
                    pmid_match = re.search(r'pubmed/(\d+)|pmid/(\d+)', url) or re.search(r'/(\d{7,})/?$', url)
                    if pmid_match:
                        pmid = pmid_match.group(1) or pmid_match.group(2)
                if pmid:
                    content = self._fetch_pubmed_full(pmid)
                    # If PubMed has PMC link, try fetching full text from PMC
                    if content and "pmc.ncbi.nlm.nih.gov" in content:
                        pmc_link = re.search(r'pmc\.ncbi\.nlm\.nih\.gov/articles/PMC(\d+)', content)
                        if pmc_link:
                            pmc_full = self._fetch_pmc_full(pmc_link.group(1))
                            if pmc_full and len(pmc_full) > 1000:
                                content = pmc_full

            # Priority 3: Try DOI-based full text
            if not content and (doi or (url and ("doi.org" in url or "dx.doi.org" in url))):
                if not doi and url:
                    doi = url.split("doi.org/")[-1].strip("/")
                if doi:
                    content = self._fetch_full_text_by_doi(doi)

            # Priority 4: Source-specific fetchers
            if not content:
                if url and ("biorxiv.org" in url or "medrxiv.org" in url):
                    content = self._fetch_biorxiv(url)
                elif url and "arxiv.org" in url:
                    content = self._fetch_arxiv(url)
                elif url and "uniprot.org" in url:
                    accession_match = re.search(r'uniprotkb?/([A-Z0-9]+)', url)
                    if accession_match:
                        content = self._fetch_uniprot_full(accession_match.group(1))
                elif url and "clinicaltrials.gov" in url:
                    content = self._fetch_clinicaltrials(url)

            # Priority 5: Generic web page content extraction
            if not content and url:
                content = self._fetch_url_content(url, max_chars)

            # Priority 6: Try Unpaywall for open access full text
            if not content and doi:
                content = self._fetch_via_unpaywall(doi)

        except Exception as exc:
            logger.warning("Full-text fetch failed for %s: %s", url or pmid or doi, exc)
            content = ""

        # NO TRUNCATION - save complete content
        self._cache[cache_key] = content
        return content

    def _fetch_pubmed_full(self, pmid: str) -> str:
        """Fetch full PubMed record via NCBI E-utilities, and try PMC for full text."""
        try:
            # Step 1: Try to get PMCID from Europe PMC
            pmcid = self._get_pmcid_from_pmid(pmid)
            if pmcid:
                full_text = self._fetch_pmc_full(pmcid)
                if full_text and len(full_text) > 3000:
                    return full_text

            # Step 2: Fallback to PubMed abstract
            response = self.session.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={
                    "db": "pubmed",
                    "id": pmid,
                    "retmode": "xml",
                    "rettype": "abstract",
                },
                timeout=30
            )
            if response.status_code != 200:
                return ""

            from xml.etree import ElementTree as ET
            root = ET.fromstring(response.text)

            parts = []
            for article in root.findall(".//PubmedArticle"):
                pmid_elem = article.find(".//PMID")
                pmid_text = pmid_elem.text if pmid_elem is not None else pmid

                title_elem = article.find(".//ArticleTitle")
                title = "".join(title_elem.itertext()).strip() if title_elem is not None else ""
                if title:
                    parts.append(f"**标题**: {title}")

                journal_elem = article.find(".//Journal/Title")
                if journal_elem is not None and journal_elem.text:
                    parts.append(f"**期刊**: {journal_elem.text.strip()}")

                pub_date_parts = []
                pub_date = article.find(".//PubDate/Year")
                if pub_date is not None and pub_date.text:
                    pub_date_parts.append(pub_date.text.strip())
                pub_date_month = article.find(".//PubDate/Month")
                if pub_date_month is not None and pub_date_month.text:
                    pub_date_parts.append(pub_date_month.text.strip())
                pub_date_day = article.find(".//PubDate/Day")
                if pub_date_day is not None and pub_date_day.text:
                    pub_date_parts.append(pub_date_day.text.strip())
                if pub_date_parts:
                    parts.append(f"**发表日期**: {'-'.join(pub_date_parts)}")

                authors = []
                for author in article.findall(".//AuthorList/Author"):
                    last_name = author.findtext("LastName", "")
                    fore_name = author.findtext("ForeName", "")
                    if last_name:
                        authors.append(f"{fore_name} {last_name}".strip())
                if authors:
                    parts.append(f"**作者**: {', '.join(authors[:10])}")

                affiliations = []
                for aff in article.findall(".//Author/AffiliationInfo/Affiliation"):
                    if aff.text and aff.text.strip() and aff.text.strip() not in affiliations:
                        affiliations.append(aff.text.strip())
                if affiliations:
                    parts.append(f"**单位**: {'; '.join(affiliations[:3])}")

                abstract_parts = []
                for abstract_text in article.findall(".//Abstract/AbstractText"):
                    label = abstract_text.get("Label", "")
                    text = "".join(abstract_text.itertext()).strip()
                    if label:
                        abstract_parts.append(f"**{label}**: {text}")
                    else:
                        abstract_parts.append(text)

                if abstract_parts:
                    parts.append(f"**摘要**:\n{' '.join(abstract_parts)}")

                keywords = []
                for keyword in article.findall(".//Keyword"):
                    if keyword.text:
                        keywords.append(keyword.text.strip())
                if keywords:
                    parts.append(f"**关键词**: {', '.join(keywords[:10])}")

                mesh_terms = []
                for mesh in article.findall(".//MeshHeading"):
                    descriptor = mesh.find("DescriptorName")
                    if descriptor is not None and descriptor.text:
                        qualifiers = mesh.findall("QualifierName")
                        qual_text = ", ".join(q.text for q in qualifiers if q.text)
                        if qual_text:
                            mesh_terms.append(f"{descriptor.text.strip()} ({qual_text})")
                        else:
                            mesh_terms.append(descriptor.text.strip())
                if mesh_terms:
                    parts.append(f"**MeSH术语**: {', '.join(mesh_terms[:8])}")

                doi_elem = article.find(".//ArticleId[@IdType='doi']")
                if doi_elem is not None and doi_elem.text:
                    parts.append(f"**DOI**: {doi_elem.text.strip()}")

                if len(parts) > 1:
                    return "\n\n".join(parts)
                return ""
        except Exception as exc:
            logger.warning("PubMed full-text fetch failed for PMID %s: %s", pmid, exc)
            return ""
        return ""

    def _get_pmcid_from_pmid(self, pmid: str) -> str:
        """Get PMCID from PMID using Europe PMC API."""
        try:
            response = self.session.get(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:{pmid}&format=json&resultType=core",
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                results = data.get("resultList", {}).get("result", [])
                if results:
                    result = results[0]
                    pmcid = result.get("pmcid", "")
                    if pmcid:
                        return pmcid.replace("PMC", "")
        except Exception as exc:
            logger.info("PMCID lookup failed for PMID %s: %s", pmid, exc)
        return ""

    def _fetch_pmc_full(self, pmcid: str) -> str:
        """Fetch full-text from PubMed Central."""
        try:
            response = self.session.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                params={
                    "db": "pmc",
                    "id": pmcid,
                    "retmode": "xml",
                    "rettype": "full",
                },
                timeout=30
            )
            if response.status_code != 200:
                return ""

            from xml.etree import ElementTree as ET
            root = ET.fromstring(response.text)

            parts = []
            title_elem = root.find(".//article-title")
            if title_elem is not None:
                title = "".join(title_elem.itertext()).strip()
                parts.append(f"**标题**: {title}")

            # Abstract
            abstract_parts = []
            for abs_para in root.findall(".//abstract/p"):
                text = "".join(abs_para.itertext()).strip()
                if text:
                    abstract_parts.append(text)
            if abstract_parts:
                parts.append(f"**摘要**:\n{' '.join(abstract_parts)}")

            # Full text sections
            for section in root.findall(".//sec"):
                title = section.find("title")
                section_title = "".join(title.itertext()).strip() if title is not None else ""
                if section_title:
                    parts.append(f"**{section_title}**:")

                section_text_parts = []
                for para in section.findall(".//p"):
                    text = "".join(para.itertext()).strip()
                    if text:
                        section_text_parts.append(text)
                
                if section_text_parts:
                    parts.append(" ".join(section_text_parts))

            # Also capture figure legends if available
            for fig in root.findall(".//fig"):
                fig_title = fig.find("title")
                if fig_title is not None:
                    title_text = "".join(fig_title.itertext()).strip()
                    caption_parts = []
                    for caption_p in fig.findall("p"):
                        caption_text = "".join(caption_p.itertext()).strip()
                        if caption_text:
                            caption_parts.append(caption_text)
                    if caption_parts:
                        parts.append(f"**图注 - {title_text}**: {' '.join(caption_parts)}")

            full_text = "\n\n".join(parts)
            return full_text if len(full_text) > 1000 else ""
        except Exception as exc:
            logger.warning("PMC full-text fetch failed for PMC%s: %s", pmcid, exc)
            return ""

    def _fetch_uniprot_full(self, accession: str) -> str:
        """Fetch full UniProt entry details."""
        try:
            response = self.session.get(
                f"https://rest.uniprot.org/uniprotkb/{accession}.txt",
                timeout=20
            )
            if response.status_code == 200:
                return response.text[:15000]

            response = self.session.get(
                f"https://rest.uniprot.org/uniprotkb/{accession}.json",
                headers={"Accept": "application/json"},
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                parts = []

                protein_name = data.get("proteinDescription", {}).get("recommendedName", {}).get("fullName", {}).get("value", "")
                if protein_name:
                    parts.append(f"## 蛋白质名称: {protein_name}")

                gene_names = []
                for gene in data.get("genes", []):
                    name = gene.get("geneName", {}).get("value", "")
                    if name:
                        gene_names.append(name)
                if gene_names:
                    parts.append(f"**基因**: {', '.join(gene_names)}")

                organism = data.get("organism", {}).get("scientificName", "")
                if organism:
                    parts.append(f"**物种**: {organism}")

                for comment in data.get("comments", []):
                    comment_type = comment.get("commentType", "")
                    texts = comment.get("texts", [])
                    if texts and comment_type in {"FUNCTION", "SUBUNIT", "SUBCELLULAR LOCATION", "PATHWAY", "TISSUE SPECIFICITY"}:
                        for text in texts:
                            content = text.get("value", "")
                            if content:
                                parts.append(f"\n### {comment_type}\n\n{content}")

                return "\n\n".join(parts)
        except Exception as exc:
            logger.warning("UniProt fetch failed for %s: %s", accession, exc)
            return ""
        return ""

    def _fetch_biorxiv(self, url: str) -> str:
        """Fetch bioRxiv/medRxiv preprint content."""
        try:
            doi_match = re.search(r'(10\.\d+/[^\s]+)', url)
            if doi_match:
                return self._fetch_by_doi(doi_match.group(1))
            return self._fetch_url_content(url, 15000)
        except Exception:
            return ""

    def _fetch_arxiv(self, url: str) -> str:
        """Fetch arXiv paper content."""
        try:
            arxiv_id_match = re.search(r'(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})', url)
            if arxiv_id_match:
                arxiv_id = arxiv_id_match.group(1)
                response = self.session.get(
                    "http://export.arxiv.org/api/query",
                    params={"id_list": f"http://arxiv.org/abs/{arxiv_id}"},
                    timeout=20
                )
                if response.status_code == 200:
                    from xml.etree import ElementTree as ET
                    root = ET.fromstring(response.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
                    parts = []
                    for entry in root.findall("atom:entry", ns):
                        title = entry.find("atom:title", ns)
                        if title is not None:
                            parts.append(f"## {title.text.strip()}")
                        summary = entry.find("atom:summary", ns)
                        if summary is not None:
                            parts.append(f"\n**摘要**: {summary.text.strip()}")
                        authors = [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns) if a.find("atom:name", ns) is not None]
                        if authors:
                            parts.append(f"\n**作者**: {', '.join(authors[:10])}")
                        published = entry.find("atom:published", ns)
                        if published is not None:
                            parts.append(f"\n**发表日期**: {published.text}")
                        doi_elem = entry.find("atom:link[@rel='related'][@title='doi']", ns)
                        if doi_elem is not None:
                            parts.append(f"\n**DOI**: {doi_elem.get('href', '')}")
                    return "\n\n".join(parts)
            return self._fetch_url_content(url, 15000)
        except Exception:
            return ""

    def _fetch_by_doi(self, doi: str) -> str:
        """Fetch article content via Crossref API."""
        try:
            response = self.session.get(
                f"https://api.crossref.org/works/{doi}",
                headers={"Accept": "application/json"},
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                message = data.get("message", {})
                parts = []

                title = message.get("title", [])
                if title:
                    parts.append(f"## {title[0]}")

                authors = message.get("author", [])
                if authors:
                    author_names = []
                    for a in authors[:10]:
                        given = a.get("given", "")
                        family = a.get("family", "")
                        if given or family:
                            author_names.append(f"{given} {family}".strip())
                    parts.append(f"**作者**: {', '.join(author_names)}")

                abstract = message.get("abstract", "")
                if abstract:
                    abstract_clean = re.sub(r'<[^>]+>', '', abstract).strip()
                    if abstract_clean:
                        parts.append(f"\n## 摘要\n\n{abstract_clean}")

                container = message.get("container-title", [])
                if container:
                    parts.append(f"\n**期刊**: {container[0]}")

                published_print = message.get("published-print", message.get("published-online", {}))
                if published_print:
                    date_parts = published_print.get("date-parts", [[]])[0]
                    if date_parts:
                        parts.append(f"\n**发表日期**: {'-'.join(str(d) for d in date_parts)}")

                if len(parts) > 1:
                    return "\n\n".join(parts)
        except Exception:
            pass
        return ""

    def _fetch_full_text_by_doi(self, doi: str) -> str:
        """Fetch full-text content by trying multiple DOI-based sources."""
        # Try 1: Crossref with full-text link
        try:
            response = self.session.get(
                f"https://api.crossref.org/works/{doi}",
                headers={"Accept": "application/json"},
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                message = data.get("message", {})
                
                # Check for full-text links
                links = message.get("link", [])
                for link in links:
                    if link.get("content-type", "").startswith("text/html"):
                        full_text_url = link.get("URL", "")
                        if full_text_url and ("nih.gov" in full_text_url or "ncbi.nlm.nih.gov" in full_text_url):
                            content = self._fetch_url_content(full_text_url, 15000)
                            if content and len(content) > 2000:
                                return content
        except Exception:
            pass

        # Try 2: Europe PMC for open access full text
        try:
            response = self.session.get(
                f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:{doi}&format=json&resultType=core",
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                results = data.get("resultList", {}).get("result", [])
                if results:
                    result = results[0]
                    pmcid = result.get("pmcid", "")
                    if pmcid:
                        content = self._fetch_pmc_full(pmcid.replace("PMC", ""))
                        if content and len(content) > 2000:
                            return content
                    
                    # Try to get full text from Europe PMC directly
                    source = result.get("source", "")
                    pmid = result.get("pmid", "")
                    if pmid and source == "MED":
                        content = self._fetch_pubmed_full(pmid)
                        if content and len(content) > 2000:
                            return content
        except Exception:
            pass

        # Try 3: Semantic Scholar for paper details
        try:
            response = self.session.get(
                f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=title,abstract,authors,year,venue,externalIds",
                headers={"Accept": "application/json"},
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                parts = []
                
                title = data.get("title", "")
                if title:
                    parts.append(f"**标题**: {title}")
                
                abstract = data.get("abstract", "")
                if abstract:
                    parts.append(f"**摘要**: {abstract}")
                
                authors = data.get("authors", [])
                if authors:
                    author_names = [a.get("name", "") for a in authors[:10] if a.get("name")]
                    parts.append(f"**作者**: {', '.join(author_names)}")
                
                year = data.get("year", "")
                venue = data.get("venue", "")
                if year or venue:
                    parts.append(f"**发表信息**: {venue} ({year})")
                
                if len(parts) > 1:
                    return "\n\n".join(parts)
        except Exception:
            pass

        # Fallback: Basic Crossref abstract
        return self._fetch_by_doi(doi)

    def _fetch_via_unpaywall(self, doi: str) -> str:
        """Try to find open access full text via Unpaywall API."""
        try:
            response = self.session.get(
                f"https://api.unpaywall.org/v2/{doi}?email=bio-agent@example.com",
                timeout=20
            )
            if response.status_code == 200:
                data = response.json()
                
                # Check if open access
                if data.get("is_oa", False):
                    # Try to get full text from OA URL
                    oa_url = data.get("best_oa_location", {}).get("url_for_pdf", "") or data.get("best_oa_location", {}).get("url", "")
                    if oa_url:
                        if "pmc.ncbi.nlm.nih.gov" in oa_url:
                            pmcid_match = re.search(r'PMC(\d+)', oa_url, re.IGNORECASE)
                            if pmcid_match:
                                content = self._fetch_pmc_full(pmcid_match.group(1))
                                if content and len(content) > 2000:
                                    return content
                        elif "biorxiv.org" in oa_url or "medrxiv.org" in oa_url:
                            content = self._fetch_biorxiv(oa_url)
                            if content and len(content) > 2000:
                                return content
                        else:
                            content = self._fetch_url_content(oa_url, 15000)
                            if content and len(content) > 2000:
                                return content

                    # Try repository URL
                    repo_url = data.get("best_oa_location", {}).get("repository_url", "")
                    if repo_url and repo_url != oa_url:
                        content = self._fetch_url_content(repo_url, 15000)
                        if content and len(content) > 2000:
                            return content

        except Exception as exc:
            logger.info("Unpaywall fetch failed for DOI %s: %s", doi, exc)
        
        return ""

    def _fetch_clinicaltrials(self, url: str) -> str:
        """Fetch ClinicalTrials.gov study details."""
        try:
            nct_match = re.search(r'(NCT\d+)', url)
            if nct_match:
                nct_id = nct_match.group(1)
                response = self.session.get(
                    f"https://clinicaltrials.gov/api/v2/studies/{nct_id}",
                    headers={"Accept": "application/json"},
                    timeout=20
                )
                if response.status_code == 200:
                    data = response.json()
                    protocol = data.get("protocolSection", {})
                    parts = []

                    identification = protocol.get("identificationModule", {})
                    parts.append(f"## {identification.get('briefTitle', 'N/A')}")

                    if identification.get("nctId"):
                        parts.append(f"\n**NCT ID**: {identification['nctId']}")

                    if identification.get("organization", {}).get("name"):
                        parts.append(f"**机构**: {identification['organization']['name']}")

                    status = protocol.get("statusModule", {})
                    if status.get("overallStatus"):
                        parts.append(f"\n**状态**: {status['overallStatus']}")

                    description = protocol.get("descriptionModule", {})
                    if description.get("briefSummary"):
                        parts.append(f"\n## 研究摘要\n\n{description['briefSummary']}")
                    if description.get("detailedDescription"):
                        parts.append(f"\n## 详细描述\n\n{description['detailedDescription']}")

                    conditions = protocol.get("conditionsModule", {})
                    if conditions.get("conditions"):
                        parts.append(f"\n**疾病/条件**: {', '.join(conditions['conditions'][:10])}")

                    if len(parts) > 1:
                        return "\n\n".join(parts)
        except Exception:
            pass
        return ""

    def _fetch_url_content(self, url: str, max_chars: int = 15000) -> str:
        """Fetch and extract main content from a web page."""
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return self._extract_main_content(response.text, max_chars)
        except Exception as exc:
            logger.warning("Web content fetch failed for %s: %s", url, exc)
            return ""

    def _extract_main_content(self, html: str, max_chars: int) -> str:
        """Extract main content from HTML with smart parsing."""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")

            for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
                tag.decompose()

            article = soup.find("article")
            if article:
                text = article.get_text("\n", strip=True)
                if len(text) > 200:
                    return self._clean_text(text, max_chars)

            main = soup.find("main")
            if main:
                text = main.get_text("\n", strip=True)
                if len(text) > 200:
                    return self._clean_text(text, max_chars)

            content_div = soup.find("div", class_=re.compile(r"content|article|post|body|entry|main", re.I))
            if content_div:
                text = content_div.get_text("\n", strip=True)
                if len(text) > 200:
                    return self._clean_text(text, max_chars)

            paragraphs = soup.find_all("p")
            if paragraphs:
                text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
                if len(text) > 200:
                    return self._clean_text(text, max_chars)

            text = soup.get_text("\n", strip=True)
            return self._clean_text(text, max_chars)
        except ImportError:
            return self._extract_text_regex(html, max_chars)
        except Exception:
            return self._extract_text_regex(html, max_chars)

    def _extract_text_regex(self, html: str, max_chars: int) -> str:
        """Fallback: extract text from HTML using regex."""
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)

        text = re.sub(r'<[^>]+>', ' ', html)
        text = unescape(text)
        text = re.sub(r'\s+', ' ', text).strip()
        return self._clean_text(text, max_chars)

    def _clean_text(self, text: str, max_chars: int) -> str:
        """Clean and format extracted text. NO TRUNCATION."""
        lines = [line.strip() for line in text.split("\n")]
        lines = [line for line in lines if line and len(line) > 10]
        result = "\n\n".join(lines)
        result = re.sub(r'\n{3,}', '\n\n', result)
        # NO TRUNCATION - return complete content
        return result
