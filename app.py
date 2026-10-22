import time
from urllib.parse import quote
 
import requests
from flask import Flask, render_template, request
from werkzeug.exceptions import HTTPException
 
app = Flask(__name__)
 
API_URL = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "SixDegreesOfWikipedia/1.0 (student project; rtseng@scu.edu)"}
 
BATCH_SIZE = 50          # Wikipedia allows 50 titles per request
MAX_CONTINUATIONS = 40   # follow-up requests allowed per batch of titles
TIME_LIMIT = 40          # seconds before the search gives up
MAX_ARTICLES = 5000      # articles examined before the search gives up
 
 
# ---------------------------------------------------------------- errors
 
class WikiError(Exception):
    """Wikipedia could not be reached or returned something unusable."""
 
 
class ArticleNotFound(Exception):
    def __init__(self, name):
        super().__init__(name)
        self.name = name
 
 
class SearchLimit(Exception):
    def __init__(self, examined):
        super().__init__(examined)
        self.examined = examined
 
 
class NoPath(Exception):
    def __init__(self, examined):
        super().__init__(examined)
        self.examined = examined
 
 
class OutOfTime(Exception):
    pass
 
 
# ----------------------------------------------------------- Wikipedia API
 
def api_get(params):
    """Call the Wikipedia API and return the decoded JSON."""
    params = {"format": "json", "formatversion": 2, **params}
    try:
        response = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as err:
        app.logger.error("Wikipedia API call failed: %r", err)
        raise WikiError("Wikipedia did not answer. Please try again in a moment.") from None
    if "error" in data:
        info = data["error"].get("info", "unknown error")
        raise WikiError(f"Wikipedia reported a problem: {info}")
    return data
 
 
def resolve_title(name):
    """Return the official title of an article (following redirects)."""
    if "|" in name or len(name) > 255:
        raise ArticleNotFound(name)
    data = api_get({"action": "query", "titles": name, "redirects": 1})
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        raise ArticleNotFound(name)
    return pages[0]["title"]
 
 
def fetch_neighbors(titles, direction, deadline):
    """Return {title: [linked titles]} for up to 50 articles at once.
 
    direction "forward": articles each title links to.
    direction "backward": articles that link to each title.
    Only regular articles are included (namespace 0).
    """
    if direction == "forward":
        key = "links"
        params = {"prop": "links", "pllimit": "max", "plnamespace": 0}
    else:
        key = "linkshere"
        params = {"prop": "linkshere", "lhlimit": "max", "lhnamespace": 0,
                  "lhprop": "title"}
    params.update(action="query", titles="|".join(titles))
 
    result = {title: [] for title in titles}
    for _ in range(MAX_CONTINUATIONS):
        if time.time() > deadline:
            raise OutOfTime
        data = api_get(params)
        for page in data.get("query", {}).get("pages", []):
            result.setdefault(page["title"], []).extend(
                item["title"] for item in page.get(key, [])
            )
        if "continue" not in data:
            break
        params.update(data["continue"])
    return result
 
 
# ------------------------------------------------------------------ search
 
def build_path(meeting, parents):
    """Join the forward half and the backward half at the meeting article."""
    chain = []
    node = meeting
    while node is not None:
        chain.append(node)
        node = parents["forward"].get(node)
    chain.reverse()
    node = parents["backward"].get(meeting)
    while node is not None:
        chain.append(node)
        node = parents["backward"].get(node)
    return chain
 
 
def find_path(start, end):
    """Return (chain of titles, number of articles examined)."""
    if start == end:
        return [start], 0
 
    deadline = time.time() + TIME_LIMIT
    parents = {"forward": {start: None}, "backward": {end: None}}
    frontiers = {"forward": [start], "backward": [end]}
    examined = 0
 
    while frontiers["forward"] and frontiers["backward"]:
        # Expand whichever side is smaller; it keeps the search small.
        if len(frontiers["forward"]) <= len(frontiers["backward"]):
            side, other = "forward", "backward"
        else:
            side, other = "backward", "forward"
 
        frontier = frontiers[side]
        next_frontier = []
        for i in range(0, len(frontier), BATCH_SIZE):
            batch = frontier[i:i + BATCH_SIZE]
            try:
                neighbors = fetch_neighbors(batch, side, deadline)
            except OutOfTime:
                raise SearchLimit(examined) from None
            examined += len(batch)
 
            for title, links in neighbors.items():
                for link in links:
                    if link in parents[side]:
                        continue
                    parents[side][link] = title
                    if link in parents[other]:
                        return build_path(link, parents), examined
                    next_frontier.append(link)
 
            if examined >= MAX_ARTICLES:
                raise SearchLimit(examined)
        frontiers[side] = next_frontier
 
    raise NoPath(examined)
 
 
# ------------------------------------------------------------------ routes
 
@app.template_filter("wiki_url")
def wiki_url(title):
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))
 
 
def home_with_error(message, start, end, status):
    return render_template("index.html", error=message, start=start, end=end), status
 
 
@app.route("/")
def home():
    return render_template("index.html", error=None, start="", end="")
 
 
@app.route("/path")
def path():
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    if not start or not end:
        return home_with_error(
            "Enter both a starting article and a target article.", start, end, 400)
 
    started = time.time()
    try:
        start_title = resolve_title(start)
        end_title = resolve_title(end)
        chain, examined = find_path(start_title, end_title)
    except ArticleNotFound as err:
        return home_with_error(
            f"Wikipedia has no article called \u201c{err.name}\u201d. "
            "Check the spelling and try again.", start, end, 404)
    except SearchLimit as err:
        return home_with_error(
            f"No chain found after looking at {err.examined:,} articles. "
            "Try two articles that are more closely related.", start, end, 504)
    except NoPath as err:
        return home_with_error(
            f"No chain exists. The search ran out of links after {err.examined:,} articles.",
            start, end, 404)
    except WikiError as err:
        return home_with_error(str(err), start, end, 502)
 
    return render_template(
        "result.html", chain=chain, examined=examined,
        seconds=time.time() - started,
    )
 
 
@app.errorhandler(HTTPException)
def handle_http_error(err):
    """Show a plain page for 404, 500 and friends instead of a traceback."""
    messages = {
        404: "That page does not exist.",
        405: "That action is not allowed on this page.",
        500: "Something went wrong on our side. Please try again.",
    }
    code = err.code or 500
    message = messages.get(code, "Something went wrong. Please try again.")
    return render_template("error.html", code=code, message=message), code
 
 
if __name__ == "__main__":
    app.run()