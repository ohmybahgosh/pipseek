import textwrap
import shutil
import requests
import string
import re
import hashlib
import bs4
import datetime
import time
import signal
import sys
from urllib.parse import quote, urlparse, parse_qs, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from textual import work
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Header, Footer, Static
from textual.binding import Binding
from rich.text import Text
from rich.panel import Panel
from rich.console import Console
from rich import box

def show_help():
    """Show help text using Rich formatting"""
    console = Console()
    console.print("\n[bold cyan]PIPSeek - PyPI Package Search Tool[/bold cyan]")
    console.print("\n[yellow]Usage:[/yellow]")
    console.print("  pipseek <search_term>")
    console.print("  pipseek \"search phrase with spaces\"")
    
    console.print("\n[yellow]Navigation Keys:[/yellow]")
    console.print("  [green]↑/k[/green]          : Scroll Up")
    console.print("  [green]↓/j[/green]          : Scroll Down")
    console.print("  [green]PageUp[/green]       : Page Up")
    console.print("  [green]PageDown[/green]     : Page Down")
    console.print("  [green]n[/green]            : Next Page of Results")
    console.print("  [green]p[/green]            : Previous Page of Results")
    console.print("  [green]q[/green]            : Quit Application")
    console.print("  [green]Ctrl+C[/green]       : Exit Immediately")
    
    console.print("\n[yellow]Examples:[/yellow]")
    console.print("  pipseek requests")
    console.print("  pipseek \"web framework\"")
    console.print("  pipseek pandas numpy")
    
    console.print("\n[yellow]Tips:[/yellow]")
    console.print("• Use quotes for multi-word searches")
    console.print("• Results show package name, description, and installation command")
    console.print("• GitHub stats are shown for packages hosted on GitHub")
    console.print("• Use Ctrl+C to exit at any time\n")

def signal_handler(sig, frame):
    """Handle interrupt signals gracefully"""
    print("\n\n[cyan]Search cancelled - Exiting PIPSeek[/cyan]")
    sys.exit(0)

# Set up signal handler
signal.signal(signal.SIGINT, signal_handler)

class StatusMessage(Static):
    """A widget to display status messages"""
    def __init__(self, message: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message

    def render(self):
        return f"[bold cyan]{self.message}[/]"

class PackageResult(Static):
    """A widget to display a package result"""
    def __init__(self, package_info: dict) -> None:
        super().__init__()
        self.package_info = package_info

    def render(self):
        pkg = self.package_info
        content = Text()
        
        # Package name and version
        content.append("🔷 ", style="bold cyan")
        content.append(pkg['name'], style="bold white")
        content.append(f" (v{pkg['version']})\n", style="bold green")
        content.append("─" * (len(pkg['name']) + len(pkg['version']) + 6) + "\n", style="dim")

        # Description
        if pkg['description'] != 'No description available':
            content.append("📖 Description:\n", style="bold yellow")
            content.append(f"{pkg['description']}\n\n", style="white")

        # Install command
        content.append("💾 Install Command:\n", style="bold yellow")
        content.append("   ")
        content.append("pip install ", style="bold bright_green on grey30")
        content.append(pkg['name'], style="bold bright_white on grey30")
        content.append("\n\n", style="bold green on black")

        # Homepage
        if pkg['homepage'] != 'N/A':
            content.append("🌍 Homepage: ", style="bold yellow")
            content.append(f"{pkg['homepage']}\n\n", style="white")

        # Last updated
        if pkg['upload_time'] != 'N/A':
            content.append("📅 Last Updated: ", style="bold yellow")
            content.append(f"{pkg['upload_time']}\n", style="white")

        # Author
        if pkg['author'] != 'N/A':
            content.append("👤 Author: ", style="bold yellow")
            content.append(f"{pkg['author']}\n", style="white")

        # GitHub stats
        github_metrics = pkg.get('github_metrics')
        if isinstance(github_metrics, dict) and 'stars' in github_metrics and 'forks' in github_metrics:
            content.append("⭐ GitHub Stats: ", style="bold yellow")
            content.append(f"{github_metrics['stars']:,} stars, ", style="white")
            content.append(f"{github_metrics['forks']:,} forks\n", style="white")

        return Panel(content, box=box.HEAVY, style="bright_magenta")

class PIPSeek(App):
    """TUI application for searching PyPI packages"""
    
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    .results-container {
        width: 1fr;
        height: 1fr;
    }

    PackageResult {
        margin: 0 1;
    }

    StatusMessage {
        text-align: center;
        margin: 1 0;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("up,k", "scroll_up", "Scroll Up"),
        ("down,j", "scroll_down", "Scroll Down"),
        ("pageup", "page_up", "Page Up"),
        ("pagedown", "page_down", "Page Down"),
        ("n", "next_page", "Next Page"),
        ("p", "prev_page", "Previous Page"),
    ]

    def __init__(self, query: str):
        super().__init__()
        self.query = query
        self.searcher = PyPIPackageSearcher()
        self.current_page = 1
        self.has_next_page = False
        self.total_results = 0
        self.current_packages = []
        self.loading = False
        self.pages_cache = {}
        self.packages_per_page = 20

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ScrollableContainer(classes="results-container")
        yield Footer()

    async def on_mount(self) -> None:
        """Called when app is mounted"""
        try:
            # Start the search
            self.search_packages()
        except Exception as e:
            self.notify(f"Error starting search: {str(e)}", severity="error")
            await self.action_quit()

    def clear_results(self):
        container = self.query_one(ScrollableContainer)
        container.remove_children()

    def get_page_range(self, num_packages):
        start_idx = (self.current_page - 1) * self.packages_per_page + 1
        end_idx = start_idx + num_packages - 1
        return start_idx, end_idx

    def display_results(self, packages, show_navigation=True):
        container = self.query_one(ScrollableContainer)
        container.remove_children()

        if not packages:
            container.mount(StatusMessage("No packages found."))
            return

        # Calculate package range
        start_idx, end_idx = self.get_page_range(len(packages))
        status = f"Showing {start_idx}-{end_idx} of {self.total_results} packages matching '{self.query}'"
        navigation = ""
        
        if show_navigation:
            if self.has_next_page:
                navigation = " (n: Next)"
            if self.current_page > 1:
                navigation += " (p: Previous)"
        
        if navigation:
            status += navigation

        # Mount status message
        container.mount(StatusMessage(status))

        # Display packages
        for package in packages:
            container.mount(PackageResult(package))

    @work(thread=True)
    def search_packages(self) -> None:
        if self.loading:
            return

        self.loading = True
        try:
            # Check cache first
            if self.current_page in self.pages_cache:
                packages = self.pages_cache[self.current_page]['packages']
                has_next = self.pages_cache[self.current_page]['has_next']
                total = self.pages_cache[self.current_page]['total']
            else:
                # Show loading message
                self.call_from_thread(self.clear_results)
                self.call_from_thread(lambda: self.query_one(ScrollableContainer).mount(
                    StatusMessage(f"Searching page {self.current_page}...")
                ))

                # Perform search
                packages, has_next, total = self.searcher.search_pypi_packages(self.query, self.current_page)
                
                # Cache results
                if packages:
                    self.pages_cache[self.current_page] = {
                        'packages': packages,
                        'has_next': has_next,
                        'total': total
                    }

            # Update state
            self.has_next_page = has_next
            self.total_results = total
            self.current_packages = packages

            # Display results
            self.call_from_thread(self.display_results, packages)

        except Exception as e:
            self.call_from_thread(self.clear_results)
            self.call_from_thread(lambda: self.query_one(ScrollableContainer).mount(
                StatusMessage(f"Error searching packages: {str(e)}")
            ))
        finally:
            self.loading = False

    def action_next_page(self) -> None:
        if not self.loading and self.has_next_page:
            self.current_page += 1
            self.search_packages()

    def action_prev_page(self) -> None:
        if not self.loading and self.current_page > 1:
            self.current_page -= 1
            self.search_packages()

    def action_scroll_up(self) -> None:
        self.query_one(ScrollableContainer).scroll_up()

    def action_scroll_down(self) -> None:
        self.query_one(ScrollableContainer).scroll_down()

    def action_page_up(self) -> None:
        self.query_one(ScrollableContainer).scroll_page_up()

    def action_page_down(self) -> None:
        self.query_one(ScrollableContainer).scroll_page_down()

    def on_key(self, event) -> None:
        # Handle Ctrl+C
        if event.key == "c" and event.modifiers == ["ctrl"]:
            self.exit()

class PyPIPackageSearcher:
    def __init__(self):
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.5",
        }
        self.github_headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "PyPIPackageSearcher"
        }
        self.timeout = 5
        self.max_workers = 20
        self.console = Console()

    def get_github_metrics(self, homepage, retries=3, delay=2):
        """Get stars and forks count from GitHub repository with retry mechanism"""
        try:
            parsed = urlparse(homepage)
            if parsed.netloc.lower() != 'github.com':
                return None
            
            path_parts = parsed.path.strip('/').split('/')
            if len(path_parts) < 2:
                return None

            owner, repo = path_parts[:2]
            repo = repo.rstrip(".git")
            
            api_url = f"https://api.github.com/repos/{owner}/{repo}"
            
            for attempt in range(retries):
                try:
                    resp = self.session.get(api_url, headers=self.github_headers, timeout=self.timeout)
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        return {
                            'stars': data.get('stargazers_count', 0),
                            'forks': data.get('forks_count', 0)
                        }
                    elif resp.status_code == 403:
                        return None  # Skip if rate limited
                except requests.exceptions.Timeout:
                    if attempt < retries - 1:
                        time.sleep(delay)
                    continue
                except (requests.exceptions.RequestException, ValueError) as e:
                    self.console.print(f"[red]Error fetching GitHub metrics: {str(e)}[/red]")
                    return None
        except Exception as e:
            self.console.print(f"[red]Error processing GitHub metrics: {str(e)}[/red]")
        return None

    def find_homepage_url(self, soup, info):
        """Find homepage URL from multiple sources"""
        try:
            # 1. Check sidebar links
            if soup:
                project_links = soup.select('.vertical-tabs__list .vertical-tabs__tab--condensed')
                for link in project_links:
                    if link.select_one('.fa-home') or any(text.lower() in link.text.lower() for text in ['homepage', 'github']):
                        return link['href']

            # 2. Check project_urls dictionary
            project_urls = info.get('project_urls', {}) or {}
            homepage_keys = ['Homepage', 'Source', 'Source Code', 'Repository', 'GitHub', 'Home']
            for key in homepage_keys:
                if key in project_urls and project_urls[key]:
                    url = project_urls[key].strip()
                    if url.lower() not in ('none', '', 'n/a'):
                        return url

            # 3. Check home_page field
            homepage = info.get('home_page', '').strip()
            if homepage and homepage.lower() not in ('none', '', 'n/a'):
                return homepage

            # 4. Check unverified section
            if soup:
                for link in soup.select('.sidebar-section.unverified a[href]'):
                    href = link['href']
                    if 'github.com' in href or any(word in link.text.lower() for word in ['source', 'home']):
                        return href

            return 'N/A'
        except Exception as e:
            self.console.print(f"[red]Error finding homepage URL: {str(e)}[/red]")
            return 'N/A'

    def get_package_details(self, package_name):
        """Get detailed information about a package"""
        try:
            # Try JSON API with retries
            json_data = None
            for _ in range(3):
                try:
                    url = f"https://pypi.org/pypi/{package_name}/json"
                    resp = self.session.get(url, timeout=self.timeout)
                    if resp.status_code == 200:
                        json_data = resp.json()
                        break
                    time.sleep(1)
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                    continue

            if not json_data or 'info' not in json_data:
                return None

            info = json_data['info']

            # Get PyPI page HTML
            soup = None
            try:
                page_url = f"https://pypi.org/project/{package_name}/"
                page_resp = self.session.get(page_url, timeout=self.timeout)
                if page_resp.status_code == 200:
                    soup = bs4.BeautifulSoup(page_resp.text, "html.parser")
            except requests.exceptions.RequestException:
                pass

            # Get homepage URL
            homepage = self.find_homepage_url(soup, info)

            # Get version and upload time
            version_str = info.get('version', 'N/A')
            upload_time = 'N/A'
            
            # Find latest release date
            all_releases = []
            for rel_info in json_data.get('releases', {}).values():
                if isinstance(rel_info, list):
                    for release in rel_info:
                        try:
                            if release.get('upload_time'):
                                release_time = datetime.datetime.strptime(
                                    release['upload_time'], '%Y-%m-%dT%H:%M:%S'
                                )
                                all_releases.append(release_time)
                        except (ValueError, TypeError):
                            continue
            
            if all_releases:
                latest_release = max(all_releases)
                upload_time = latest_release.strftime('%Y-%m-%d')

            # Get author information
            author = info.get('author', '')
            if soup and (not author or author.lower() in ('none', 'unknown', '', 'n/a')):
                author_elem = soup.select_one('li span:-soup-contains("Author")')
                if author_elem:
                    author = author_elem.get_text().replace('Author:', '').strip()
                    author_link = author_elem.select_one('a[href^="mailto:"]')
                    if author_link:
                        author = author_link.get_text().strip()

            if not author or author.lower() in ('none', 'unknown', '', 'n/a'):
                author = 'N/A'

            # Get GitHub metrics
            github_metrics = None
            if homepage != 'N/A' and 'github.com' in homepage.lower():
                github_metrics = self.get_github_metrics(homepage)

            # Get description
            description = info.get('summary', '').strip()
            if not description or description.lower() in ('none', 'no description', 'n/a'):
                description = 'No description available'

            return {
                'name': package_name,
                'version': version_str,
                'description': description,
                'upload_time': upload_time,
                'homepage': homepage,
                'author': author,
                'github_metrics': github_metrics
            }

        except Exception as e:
            self.console.print(f"[red]Error processing package {package_name}: {str(e)}[/red]")
            return None

    def solve_pow_challenge(self, url):
        """Solve PyPI's proof of work challenge"""
        try:
            resp = self.session.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code != 200:
                return False

            pattern = re.compile(r"/(.*)/script.js")
            path_matches = pattern.findall(resp.text)
            if not path_matches:
                return True

            path = path_matches[0]
            script_url = f"https://pypi.org/{path}/script.js"
            
            resp = self.session.get(script_url, timeout=self.timeout)
            if resp.status_code != 200:
                return False

            pattern = re.compile(
                r'init\(\[\{"ty":"pow","data":\{"base":"(.+?)","hash":"(.+?)","hmac":"(.+?)","expires":"(.+?)"\}\}\], "(.+?)"'
            )
            matches = pattern.findall(resp.text)
            if not matches:
                return True

            base, hash_val, hmac, expires, token = matches[0]
            answer = None
            
            # Solve challenge
            for c1 in string.ascii_letters + string.digits:
                for c2 in string.ascii_letters + string.digits:
                    if hashlib.sha256(f"{base}{c1}{c2}".encode()).hexdigest() == hash_val:
                        answer = c1 + c2
                        break
                if answer:
                    break

            if not answer:
                return False

            # Submit solution
            back_url = f"https://pypi.org/{path}/fst-post-back"
            data = {
                "token": token,
                "data": [{"ty": "pow", "base": base, "answer": answer, "hmac": hmac, "expires": expires}]
            }
            resp = self.session.post(back_url, json=data, timeout=self.timeout)
            return resp.status_code == 200

        except Exception as e:
            self.console.print(f"[red]Error solving PoW challenge: {str(e)}[/red]")
            return False

    def search_pypi_packages(self, query, page=1):
        """Search PyPI packages with improved pagination support"""
        try:
            encoded_query = quote(query)
            url = f"https://pypi.org/search/?q={encoded_query}&page={page}"
            
            if not self.solve_pow_challenge(url):
                self.console.print("[yellow]Warning: Failed to solve PyPI proof of work challenge[/yellow]")
                return [], False, 0

            resp = self.session.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                return [], False, 0

            soup = bs4.BeautifulSoup(resp.text, "html.parser")
            
            # Get total number of results
            total_results = 0
            results_text = soup.select_one('.split-layout p strong')
            if results_text:
                try:
                    total_text = results_text.text.strip()
                    total_results = int(''.join(c for c in total_text if c.isdigit()))
                except (ValueError, AttributeError):
                    pass
            
            # Get package names
            package_names = []
            for snippet in soup.select(".package-snippet"):
                name_elem = snippet.select_one(".package-snippet__name")
                if name_elem and name_elem.text.strip():
                    package_names.append(name_elem.text.strip())
            
            if not package_names:
                return [], False, total_results

            # Check pagination
            has_next_page = False
            pagination = soup.select_one('.button-group--pagination')
            if pagination:
                next_button = pagination.find('a', string='Next')
                has_next_page = bool(next_button and 'button--disabled' not in next_button.get('class', []))

            # Process packages concurrently
            packages = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_package = {
                    executor.submit(self.get_package_details, name): name 
                    for name in package_names
                }
                for future in as_completed(future_to_package):
                    try:
                        package_info = future.result()
                        if package_info:
                            packages.append(package_info)
                    except Exception as e:
                        self.console.print(f"[red]Error processing package: {str(e)}[/red]")

            return packages, has_next_page, total_results

        except Exception as e:
            self.console.print(f"[red]Error searching packages: {str(e)}[/red]")
            return [], False, 0


def main():
    try:
        import argparse
        parser = argparse.ArgumentParser(
            description='Search PyPI packages with an interactive interface.',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=textwrap.dedent("""
                Examples:
                  %(prog)s requests          Search for 'requests' package
                  %(prog)s "web framework"   Search using multiple keywords
                
                Navigation:
                  Up/k        : Scroll Up
                  Down/j      : Scroll Down
                  PageUp      : Page Up
                  PageDown    : Page Down
                  n          : Next Page
                  p          : Previous Page
                  q          : Quit
                  Ctrl+C     : Exit Immediately
                
                Tips:
                  • Use quotes for multi-word searches
                  • Results show package details and GitHub stats when available
                  • 'n' and 'p' keys navigate between result pages
                """)
        )
        parser.add_argument('query', nargs='+', help='Search terms (use quotes for phrases)')
        parser.add_argument('-v', '--version', action='version', version='%(prog)s 1.0.0')
        
        if len(sys.argv) == 1:
            show_help()
            sys.exit(0)
            
        args = parser.parse_args()
        query = ' '.join(args.query)

        app = PIPSeek(query)
        app.run(mouse=False)

    except KeyboardInterrupt:
        console = Console()
        console.print("\n\n[cyan]Search cancelled - Exiting PIPSeek[/cyan]")
        sys.exit(0)
    except Exception as e:
        console = Console()
        console.print(f"\n[red]Error: {str(e)}[/red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
