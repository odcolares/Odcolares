#!/usr/bin/env python3
"""Gera issues-rascunho para posts do LinkedIn a partir de PRs mergeados.

Fase C (sem autenticacao no LinkedIn): a postagem e MANUAL, feita por voce.
Esta action so prepara o texto pronto para copiar, numa issue do repo de perfil.

Garantias embutidas:
- Busca transversal usa API publica SEM token -> so enxerga repos publicos.
- Somente PRs de repositorios proprios (owner == AUTHOR).
- Filtro por label (REQUIRED_LABEL) controla o que vira rascunho.
- Dedupe por marcador <!-- PR_URL: ... --> no corpo das issues.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
AUTHOR = "odcolares"
REQUIRED_LABEL = "linkedin-post"  # vazio = todos os PRs mergeados
DRAFT_LABEL = "linkedin-draft"
DRAFT_LABEL_COLOR = "1D76DB"
DRAFT_LABEL_DESC = "Rascunho de post para LinkedIn (gerado pela Action)"
MAX_DRAFTS = 3
DEFAULT_DAYS = 7
EXCERPT_LIMIT = 400

HASHTAGS = {
    "rendaextracupuns": ["#typescript", "#nodedjs", "#automacao", "#bots"],
    "businessai": ["#python", "#fastapi", "#ia", "#dados"],
    "condominio": ["#typescript", "#express", "#jwt", "#gestao"],
    "clinica-saas": ["#python", "#django", "#saas", "#multitenant"],
}


def http(method, url, token=None, data=None):
    """GET/POST na API do GitHub. `data` e um dict (JSON)."""
    headers = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "linkedin-draft-bot"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:300]}")


def excerpt(text):
    """Primeira descricao utilizavel do corpo do PR, limitada."""
    if not text or not text.strip():
        return "Sem descricao escrita — veja os detalhes direto no PR."
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if keep:
                break
            continue
        if s.startswith(("#", "![", "[![", "---")):
            continue
        keep.append(s)
        if sum(len(x) for x in keep) > EXCERPT_LIMIT:
            break
    out = " ".join(keep)
    return (out[:EXCERPT_LIMIT] + "…") if len(out) > EXCERPT_LIMIT else out


def hashtags(repo_name):
    tags = HASHTAGS.get(repo_name.lower(), ["#dev"])
    return " ".join(tags + ["#github"])


def find_prs(days, require_label):
    """Busca PRs mergeados (API publica, sem token)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    q = f"is:pr is:merged author:{AUTHOR} merged:>={since}"
    if require_label and REQUIRED_LABEL:
        q += f" label:{REQUIRED_LABEL}"
    url = f"{API}/search/issues?" + urllib.parse.urlencode(
        {"q": q, "per_page": "50", "sort": "updated", "order": "desc"})
    result = http("GET", url)  # sem token: somente dados publicos
    return result.get("items", []) if result else []


def existing_draft_markers(token, repo):
    """Marcadores <!-- PR_URL: ... --> de issues ja criadas (abertas e fechadas)."""
    markers = set()
    page = 1
    while page <= 3:
        url = (f"{API}/repos/{repo}/issues?"
               + urllib.parse.urlencode({"labels": DRAFT_LABEL, "state": "all",
                                         "per_page": "100", "page": page}))
        issues = http("GET", url, token=token) or []
        if not issues:
            break
        for issue in issues:
            for line in issue.get("body", "").splitlines():
                if line.startswith("<!-- PR_URL:"):
                    markers.add(line.replace("<!-- PR_URL:", "").replace("-->", "").strip())
        if len(issues) < 100:
            break
        page += 1
    return markers


def ensure_draft_label(token, repo):
    if http("GET", f"{API}/repos/{repo}/labels/{DRAFT_LABEL}", token=token) is None:
        http("POST", f"{API}/repos/{repo}/labels", token=token,
             data={"name": DRAFT_LABEL, "color": DRAFT_LABEL_COLOR,
                   "description": DRAFT_LABEL_DESC})


def build_issue_body(pr, pull):
    repo_name = pr["repository_url"].rsplit("/", 1)[-1]
    owner = pr["repository_url"].split("/")[-2]
    pr_url = pr["html_url"]
    title = pr["title"].strip() or "(sem titulo)"
    text = (
        "> 🤖 **Rascunho automatico** — revise, edite se quiser e **copie** o bloco "
        "abaixo para postar no LinkedIn.\n"
        "> ✋ A postagem e **manual**, feita por voce (por design: evita spam).\n\n"
        "## 📋 Texto do post — copie ate o fim do bloco\n\n"
        "```\n"
        "🚀 Mais um merge no GitHub!\n\n"
        f"📌 {title}\n"
        f"📝 {excerpt(pull.get('body'))}\n\n"
        f"💻 {owner}/{repo_name}\n"
        f"🔗 {pr_url}\n\n"
        f"{hashtags(repo_name)}\n"
        "```\n\n"
        "---\n\n"
        "## ✅ Checklist\n"
        "- [ ] Postei no LinkedIn\n"
        "- [ ] Fecho esta issue quando concluir\n\n"
        "<details><summary>Metadados</summary>\n\n"
        f"- PR: {pr_url}\n"
        f"- Merge: {pull.get('merged_at', 'n/d')}\n"
        f"- Repo: {owner}/{repo_name}\n\n"
        "</details>\n\n"
        f"<!-- PR_URL: {pr_url} -->"
    )
    short_title = (title[:70] + "…") if len(title) > 70 else title
    return f"📥 Post LinkedIn: {repo_name} — {short_title}", text


def main():
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")  # odcolares/Odcolares
    if not token or not repo:
        sys.exit("GH_TOKEN e GITHUB_REPOSITORY sao obrigatorios.")

    days = int(os.environ.get("DAYS", DEFAULT_DAYS))
    require_label = os.environ.get("REQUIRE_LABEL", "true").lower() == "true"

    summary = ["## 📬 LinkedIn Draft Queue", "",
               f"- Janela: {days} dias | Label obrigatoria: **{require_label}**", ""]

    prs = find_prs(days, require_label)
    summary.append(f"- PRs mergeados encontrados (publicos): **{len(prs)}**")

    markers = existing_draft_markers(token, repo)
    ensure_draft_label(token, repo)

    created = 0
    for pr in prs:
        if created >= MAX_DRAFTS:
            summary.append(f"- ⚠️ Limite de {MAX_DRAFTS} rascunhos por execucao atingido.")
            break
        owner = pr["repository_url"].split("/")[-2]
        name = pr["repository_url"].rsplit("/", 1)[-1]
        if owner != AUTHOR:
            continue
        if pr["html_url"] in markers:
            continue
        # Duplo check de visibilidade publica (busca sem token ja garante)
        repo_info = http("GET", f"{API}/repos/{owner}/{name}")
        if not repo_info or repo_info.get("private"):
            summary.append(f"- 🔒 Pulado (privado/inacessivel): {owner}/{name}")
            continue
        pull = http("GET", f"{API}/repos/{owner}/{name}/pulls/{pr['number']}")
        if pull is None:
            continue
        title, body = build_issue_body(pr, pull)
        http("POST", f"{API}/repos/{repo}/issues", token=token,
             data={"title": title, "body": body, "labels": [DRAFT_LABEL]})
        markers.add(pr["html_url"])
        created += 1
        summary.append(f"- ✅ Rascunho criado: [{title}](https://github.com/{repo}/issues)")

    summary.append(f"- 📝 Issues-rascunho criadas nesta execucao: **{created}**")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(summary) + "\n")
    print("\n".join(summary))


if __name__ == "__main__":
    main()



