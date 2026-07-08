import asyncio
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, List
from crawlee.crawlers import PlaywrightCrawler
from crawlee import Request
from playwright.async_api import Page, ElementHandle
import pandas as pd


class GoogleMapsScraper:
    """
    Scraper for extracting business listing data from Google Maps.
    Phase 1: Scrolls search results and enqueues every business detail page.
    Phase 2: Visits each detail page to extract full address, phone, and more.
    Appends each run's data to a new sheet in the Excel file.
    """

    def __init__(self, headless: bool = True, timeout_minutes: int = 10):
        self.crawler = PlaywrightCrawler(
            headless=headless,
            request_handler_timeout=timedelta(minutes=timeout_minutes),
        )
        self.results: List[Dict] = []
        self.processed_urls: Set[str] = set()
        self.search_query: str = ""
        self.detail_count: int = 0

    async def setup_crawler(self) -> None:
        """Configure the crawler with a single default handler that routes by URL."""
        self.crawler.router.default_handler(self._handle_request)

    async def _handle_request(self, context) -> None:
        """Route each request to the correct handler based on URL pattern."""
        url = context.request.url
        if "/place/" in url:
            await self._handle_detail_page(context)
        else:
            await self._handle_search_results(context)

    async def _handle_search_results(self, context) -> None:
        """Scroll the search results feed and enqueue every business detail page."""
        page = context.page
        context.log.info(f"\n{'='*50}")
        context.log.info(f"SEARCH PHASE: {context.request.url}")
        context.log.info(f"{'='*50}\n")

        await page.wait_for_selector(".Nv2PK", timeout=30000)
        await page.wait_for_timeout(2000)

        enqueued = 0

        while True:
            listings = await page.query_selector_all(".Nv2PK")
            batch_new = 0

            for listing in listings:
                link_el = await listing.query_selector("a.hfpxzc")
                if not link_el:
                    continue

                href = await link_el.get_attribute("href")
                if not href or href in self.processed_urls:
                    continue

                self.processed_urls.add(href)

                # Grab the name from the search card so we can pass it to the detail handler
                name_el = await listing.query_selector(".qBF1Pd")
                name = await name_el.inner_text() if name_el else None

                # Enqueue the detail page using proper Request object
                try:
                    req = Request.from_url(
                        href,
                        user_data={"name_from_search": name}
                    )
                    await context.add_requests([req])
                    enqueued += 1
                    batch_new += 1
                    context.log.info(f"Enqueued detail page: {name}")
                except Exception as e:
                    context.log.warning(f"Failed to enqueue {href}: {e}")

            # Scroll feed to load more results
            has_more = await self._load_more_items(page)
            if batch_new == 0 and not has_more:
                break

        context.log.info(f"\nSearch phase complete. {enqueued} detail pages queued.")

    async def _handle_detail_page(self, context) -> None:
        """Visit a single business page and extract full details."""
        page = context.page
        url = context.request.url

        # Retrieve name passed from search results (if available)
        name = None
        user_data = getattr(context.request, "user_data", None) or {}
        if isinstance(user_data, dict):
            name = user_data.get("name_from_search")

        # Wait for detail page to render
        await page.wait_for_timeout(3000)

        try:
            # --- Name (fallback if not passed from search) ---
            if not name:
                for sel in ("h1", '[role="main"] h1', ".lMbq3e"):
                    el = await page.query_selector(sel)
                    if el:
                        text = await el.inner_text()
                        if text:
                            name = text.strip()
                            break

            # --- Address ---
            address = None
            address_selectors = [
                'button[data-item-id="address"]',
                '[data-item-id="address"]',
                'button[aria-label*="Address"]',
                '[data-tooltip="Copy address"]',
                'div.rogA2c',
            ]
            for sel in address_selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and len(text) > 5:
                        address = text.strip()
                        break

            # --- Phone Number ---
            phone = None
            phone_selectors = [
                'button[data-item-id^="phone"]',
                '[data-item-id^="phone"]',
                'button[aria-label*="Phone"]',
                'a[href^="tel:"]',
            ]
            for sel in phone_selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and any(c.isdigit() for c in text):
                        phone = text.strip()
                        break
                    aria = await el.get_attribute("aria-label")
                    if aria and any(c.isdigit() for c in aria):
                        phone = aria.strip()
                        break

            # --- Rating ---
            rating = None
            rating_selectors = [
                'div.F7nice span:first-child',
                'span[aria-label*="star"]',
                'div[role="main"] span[aria-label*="stars"]',
            ]
            for sel in rating_selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and any(c.isdigit() for c in text):
                        rating = text.strip()
                        break

            # --- Reviews Count ---
            reviews = None
            reviews_selectors = [
                'div.F7nice span + span',
                'button[aria-label*="review"]',
                'span[aria-label*="reviews"]',
            ]
            for sel in reviews_selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and ("review" in text.lower() or "(" in text):
                        reviews = text.strip().strip("()")
                        break

            # --- Website ---
            website = None
            website_selectors = [
                'button[data-item-id="authority"]',
                'a[data-item-id="authority"]',
                'a[aria-label*="Website"]',
            ]
            for sel in website_selectors:
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    if href and "google.com" not in href:
                        website = href
                        break

            # --- Opening Hours ---
            hours = None
            hours_el = await page.query_selector('button[data-item-id="oh"]')
            if hours_el:
                hours = await hours_el.inner_text()

            # --- Category ---
            category = None
            category_selectors = [
                'button[jsaction*="category"]',
                'span[aria-label*="Category"]',
                'div[role="main"] div span',
            ]
            for sel in category_selectors:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text and text != name and len(text) < 60:
                        category = text.strip()
                        break

            # --- Price Level ---
            price = None
            price_el = await page.query_selector('span[aria-label*="Price"]')
            if price_el:
                price = await price_el.inner_text()

            place_data = {
                "name": name,
                "category": category,
                "rating": rating,
                "reviews": reviews,
                "price": price,
                "address": address,
                "phone": phone,
                "website": website,
                "hours": hours,
                "url": url,
            }

            self.results.append(place_data)
            self.detail_count += 1
            context.log.info(f"Extracted: {name} | {address} | {phone}")

        except Exception as e:
            context.log.exception(f"Error on detail page {url}")

    async def _load_more_items(self, page: Page) -> bool:
        """Scroll the search results feed to trigger lazy loading."""
        try:
            feed = await page.query_selector('div[role="feed"]')
            if not feed:
                return False
            prev_scroll = await feed.evaluate("(element) => element.scrollTop")
            await feed.evaluate("(element) => element.scrollTop += 800")
            await page.wait_for_timeout(2000)
            new_scroll = await feed.evaluate("(element) => element.scrollTop")
            if new_scroll <= prev_scroll:
                return False
            await page.wait_for_timeout(1000)
            return True
        except Exception:
            return False

    def _sanitize_sheet_name(self, name: str) -> str:
        """Ensure Excel-compatible sheet name (max 31 chars, no forbidden chars)."""
        name = re.sub(r'[\\/*?:[\]]', '_', name)
        return name[:31]

    def export_to_excel(self, filename: str = "gmap_data.xlsx", sheet_name: Optional[str] = None) -> None:
        """Export scraped data to a new sheet in the Excel file."""
        if not self.results:
            print("No data to export.")
            return

        df = pd.DataFrame(self.results)

        column_order = [
            "name", "category", "rating", "reviews", "price",
            "address", "phone", "website", "hours", "url"
        ]
        available_columns = [c for c in column_order if c in df.columns]
        for c in df.columns:
            if c not in available_columns:
                available_columns.append(c)
        df = df[available_columns]

        if sheet_name is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            query_slug = self.search_query.replace(" ", "_")[:20] if self.search_query else "data"
            sheet_name = f"{query_slug}_{timestamp}"

        sheet_name = self._sanitize_sheet_name(sheet_name)
        file_exists = os.path.exists(filename)

        try:
            if file_exists:
                with pd.ExcelWriter(
                    filename,
                    engine='openpyxl',
                    mode='a',
                    if_sheet_exists='new'
                ) as writer:
                    df.to_excel(writer, index=False, sheet_name=sheet_name)
                    worksheet = writer.sheets[sheet_name]
                    self._adjust_column_widths(worksheet)
            else:
                with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name=sheet_name)
                    worksheet = writer.sheets[sheet_name]
                    self._adjust_column_widths(worksheet)

            print(f"\n{'='*50}")
            print(f"EXPORT SUCCESSFUL")
            print(f"{'='*50}")
            print(f"File   : {filename}")
            print(f"Sheet  : {sheet_name}")
            print(f"Records: {len(df)}")
            print(f"{'='*50}")

        except Exception as e:
            print(f"Error exporting to Excel: {e}")

    def _adjust_column_widths(self, worksheet) -> None:
        """Auto-fit column widths for readability."""
        for column in worksheet.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except:
                    pass
            adjusted_width = min(max_length + 2, 60)
            worksheet.column_dimensions[column_letter].width = adjusted_width

    async def run(self, search_query: str, output_file: str = "gmap_data.xlsx", sheet_name: Optional[str] = None) -> None:
        """Run the full scrape: search results → detail pages → Excel export."""
        self.search_query = search_query
        self.results = []
        self.processed_urls = set()
        self.detail_count = 0

        try:
            await self.setup_crawler()
            start_url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"
            await self.crawler.run([start_url])
            self.export_to_excel(output_file, sheet_name)
        except Exception as e:
            print(f"Error running scraper: {e}")


def get_user_input() -> str:
    """Interactive prompt for the search query."""
    print("\n" + "="*55)
    print("   Google Maps Deep Scraper")
    print("   (Visits every business page for full details)")
    print("="*55)
    print("Enter your search query (e.g., 'barber in new york')")
    print("Type 'quit' or press Ctrl+C to exit")
    print("-"*55)

    while True:
        query = input("\nSearch query: ").strip()
        if query.lower() in ('quit', 'exit', 'q'):
            print("Exiting...")
            sys.exit(0)
        if query:
            return query
        print("Query cannot be empty. Please try again.")


async def main():
    scraper = GoogleMapsScraper(headless=True, timeout_minutes=10)
    output_file = "gmap_data.xlsx"

    while True:
        try:
            search_query = get_user_input()
            await scraper.run(search_query, output_file)

            print("\n" + "-"*55)
            again = input("Run another search? (y/n): ").strip().lower()
            if again not in ('y', 'yes'):
                print("Goodbye!")
                break

        except KeyboardInterrupt:
            print("\n\nInterrupted by user. Exiting...")
            break
        except Exception as e:
            print(f"\nUnexpected error: {e}")
            retry = input("Try again? (y/n): ").strip().lower()
            if retry not in ('y', 'yes'):
                break


if __name__ == "__main__":
    asyncio.run(main())