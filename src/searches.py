import dbm.dumb
import json
import logging
import shelve
import time as time_module
from datetime import date, timedelta
from enum import Enum, auto
from itertools import cycle
from random import randint, random, shuffle
from time import sleep, time
from typing import Final

import requests
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from src.browser import Browser
from src.utils import CONFIG, getProjectRoot, makeRequestsSession


class RetriesStrategy(Enum):
    """
    method to use when retrying
    """

    EXPONENTIAL = auto()
    """
    an exponentially increasing `base_delay_in_seconds` between attempts
    """
    CONSTANT = auto()
    """
    the default; a constant `base_delay_in_seconds` between attempts
    """


class Searches:
    maxRetries: Final[int] = CONFIG.retries.max
    """
    the max amount of retries to attempt
    """
    baseDelay: Final[float] = CONFIG.get("retries.base_delay_in_seconds")
    """
    how many seconds to delay
    """
    # retriesStrategy = Final[  # todo Figure why doesn't work with equality below
    retriesStrategy = RetriesStrategy[CONFIG.retries.strategy]

    def __init__(self, browser: Browser):
        self.browser = browser
        self.webdriver = browser.webdriver

        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.googleTrendsShelf.__exit__(None, None, None)

    def getGoogleTrends(self, words_count: int) -> list[str]:
        """
        Retrieves Google Trends search terms via the new API (last 48 hours).
        """
        logging.debug("Starting Google Trends fetch (last 48 hours)...")
        search_terms: list[str] = []
        session = makeRequestsSession()
        
        url = "https://trends.google.com/_/TrendsUi/data/batchexecute"
        payload = f'f.req=[[[i0OFE,"[null, null, \\"{self.browser.localeGeo}\\", 0, null, 48]"]]]'
        headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        
        logging.debug(f"Sending POST request to {url}")
        try:
            response = session.post(url, headers=headers, data=payload)
            response.raise_for_status()
            logging.debug("Response received from Google Trends API")
        except requests.RequestException as e:
            logging.error(f"Error fetching Google Trends: {e}")
            return []

        trends_data = self.extract_json_from_response(response.text)
        if not trends_data:
            logging.error("Failed to extract JSON from Google Trends response")
            return []
    
        logging.debug("JSON successfully extracted. Processing root terms...")
    
        # Process only the first element in each item
        root_terms = []
        for item in trends_data:
            try:
                topic = item[0]
                root_terms.append(topic)
            except Exception as e:
                logging.warning(f"Error processing an item: {e}")
                continue
    
        logging.debug(f"Extracted {len(root_terms)} root trend entries")
    
        # Convert to lowercase and remove duplicates
        search_terms = list(set(term.lower() for term in root_terms))
        logging.debug(f"Found {len(search_terms)} unique search terms")
    
        if words_count < len(search_terms):
            logging.debug(f"Limiting search terms to {words_count} items")
            search_terms = search_terms[:words_count]
    
        logging.debug("Google Trends fetch complete")
        return search_terms

    def extract_json_from_response(self, text: str):
        """
        Extracts the nested JSON object from the API response.
        """
        logging.debug("Extracting JSON from API response")
        for line in text.splitlines():
            trimmed = line.strip()
            if trimmed.startswith('[') and trimmed.endswith(']'):
                try:
                    intermediate = json.loads(trimmed)
                    data = json.loads(intermediate[0][2])
                    logging.debug("JSON extraction successful")
                    return data[1]
                except Exception as e:
                    logging.warning(f"Error parsing JSON: {e}")
                    continue
        logging.error("No valid JSON found in response")
        return None

    def getRelatedTerms(self, term: str) -> list[str]:
        # Function to retrieve related terms from Bing API
        relatedTerms: list[str] = (
            makeRequestsSession()
            .get(
                f"https://api.bing.com/osjson.aspx?query={term}",
                headers={"User-agent": self.browser.userAgent},
            )
            .json()[1]
        )  # todo Wrap if failed, or assert response?
        if not relatedTerms:
            return [term]
        return relatedTerms

    def bingSearches(self) -> None:
        # Function to perform Bing searches
        logging.info(
            f"[BING] Starting {self.browser.browserType.capitalize()} Edge Bing searches..."
        )

        # Reset browser state before starting searches
        try:
            self.browser.utils.goToSearch()
            time_module.sleep(5)  # Give the page time to load
            
            # Clear cookies and cache periodically (once at the beginning)
            try:
                self.webdriver.execute_script('window.localStorage.clear();')
                self.webdriver.execute_script('window.sessionStorage.clear();')
                logging.info("[BING] Cleared browser storage")
            except Exception as e:
                logging.debug(f"[BING] Failed to clear storage: {e}")
        except Exception as e:
            logging.error(f"[BING] Failed to navigate to search page: {e}")
            return

        # Track overall search success
        success_count = 0
        fail_count = 0
        max_fails = 5  # Maximum consecutive failures before giving up

        while True:
            try:
                desktopAndMobileRemaining = self.browser.getRemainingSearches(
                    desktopAndMobile=True
                )
                logging.info(f"[BING] Remaining searches={desktopAndMobileRemaining}")
                
                if (
                    self.browser.browserType == "desktop"
                    and desktopAndMobileRemaining.desktop == 0
                ) or (
                    self.browser.browserType == "mobile"
                    and desktopAndMobileRemaining.mobile == 0
                ):
                    break

                if fail_count >= max_fails:
                    logging.error(f"[BING] Reached maximum consecutive failures ({max_fails}). Stopping searches.")
                    break

                if desktopAndMobileRemaining.getTotal() > len(self.googleTrendsShelf):
                    logging.debug(
                        f"google_trends before load = {list(self.googleTrendsShelf.items())}"
                    )
                    trends = self.getGoogleTrends(desktopAndMobileRemaining.getTotal())
                    shuffle(trends)
                    for trend in trends:
                        self.googleTrendsShelf[trend] = None
                    logging.debug(
                        f"google_trends after load = {list(self.googleTrendsShelf.items())}"
                    )

                # Try to perform a search
                search_result = self.bingSearch()
                
                if search_result:
                    success_count += 1
                    fail_count = 0  # Reset fail counter on success
                    
                    # After successful searches, remove the term from shelf
                    if list(self.googleTrendsShelf.keys()):
                        del self.googleTrendsShelf[list(self.googleTrendsShelf.keys())[0]]
                    
                    # Add longer delay between successful searches
                    delay = randint(15, 30) + random() * 10
                    logging.info(f"[BING] Search successful. Waiting {int(delay)} seconds before next search.")
                    sleep(delay)
                else:
                    fail_count += 1
                    logging.warning(f"[BING] Search failed. Consecutive failures: {fail_count}/{max_fails}")
                    
                    # If multiple failures, try some recovery actions
                    if fail_count >= 3:
                        try:
                            logging.info("[BING] Attempting recovery actions...")
                            # Clear cookies
                            self.webdriver.delete_all_cookies()
                            time_module.sleep(2)
                            
                            # Navigate to Bing homepage again
                            self.browser.utils.goToSearch()
                            time_module.sleep(10)
                            
                            # Check if we need to log in again
                            if not self.browser.utils.isLoggedIn():
                                logging.warning("[BING] Session lost, need to log in again")
                                # You might want to add code to log in again here
                                return
                        except Exception as e:
                            logging.error(f"[BING] Recovery action failed: {e}")
                    
                    # Add increasing delay between failed searches
                    delay = 30 + (30 * fail_count) + (random() * 30)
                    logging.info(f"[BING] Waiting {int(delay)} seconds before retry.")
                    sleep(delay)
            
            except Exception as e:
                logging.error(f"[BING] Unexpected error during search loop: {e}")
                fail_count += 1
                sleep(randint(20, 40))

        logging.info(
            f"[BING] Finished {self.browser.browserType.capitalize()} Edge Bing searches! "
            f"Completed {success_count} searches successfully."
        )

    def bingSearch(self) -> bool:
        """Perform a single Bing search. Returns True if successful, False otherwise."""
        try:
            pointsBefore = self.browser.utils.getAccountPoints()
            
            # Initialize tracking for consecutive failures
            if not hasattr(self, 'consecutive_failures'):
                self.consecutive_failures = 0
            if not hasattr(self, 'max_wait'):
                self.max_wait = 120  # Maximum wait time in seconds

            # Get search terms
            if not list(self.googleTrendsShelf.keys()):
                logging.error("[BING] No search terms available")
                return False
                
            rootTerm = list(self.googleTrendsShelf.keys())[0]
            terms = self.getRelatedTerms(rootTerm)
            if not terms:
                terms = [rootTerm]  # Fallback to root term if no related terms
            
            logging.debug(f"terms={terms}")
            termsCycle = cycle(terms)
            baseDelay = Searches.baseDelay
            logging.debug(f"rootTerm={rootTerm}")

            # Add randomized initial delay
            time_module.sleep(randint(5, 10) + random() * 3)
            
            # Attempt the search
            for i in range(min(self.maxRetries + 1, 3)):  # Limit retries to avoid excessive loops
                if i != 0:
                    sleepTime = baseDelay * (1 + random()) if i == 1 else baseDelay * 2 * (1 + random())
                    logging.debug(f"[BING] Search attempt {i+1}, sleeping {sleepTime:.1f} seconds...")
                    sleep(sleepTime)

                try:
                    # Make sure we're on the search page
                    current_url = self.webdriver.current_url
                    if "bing.com" not in current_url or "search" not in current_url.lower():
                        logging.info("[BING] Navigating to search page")
                        self.browser.utils.goToSearch()
                        time_module.sleep(5)
                    
                    # Find and interact with the search box
                    try:
                        searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=20)
                    except TimeoutException:
                        # Try alternative selectors if the main one fails
                        try:
                            searchbar = self.webdriver.find_element(By.NAME, "q")
                        except:
                            try:
                                searchbar = self.webdriver.find_element(By.XPATH, "//input[@type='search']")
                            except:
                                logging.error("[BING] Could not find search bar")
                                return False
                    
                    # Clear the search box
                    searchbar.clear()
                    time_module.sleep(random() * 1.5)
                    
                    # Get the next search term
                    term = next(termsCycle)
                    logging.info(f"[BING] Searching for: {term}")
                    
                    # Type the term with human-like delays
                    for char in term:
                        searchbar.send_keys(char)
                        time_module.sleep(0.05 + random() * 0.15)
                    
                    # Short pause before submitting
                    time_module.sleep(0.5 + random())
                    
                    # Submit the search
                    try:
                        searchbar.submit()
                    except:
                        # Alternative: press Enter
                        searchbar.send_keys(Keys.RETURN)
                    
                    # Wait for results to load
                    time_module.sleep(5 + random() * 3)
                    
                    # Check for error page
                    page_source = self.webdriver.page_source.lower()
                    if any(phrase in page_source for phrase in [
                        "it's not you, it's us", 
                        "isn't available right now",
                        "something went wrong",
                        "this page isn't available"
                    ]):
                        self.consecutive_failures += 1
                        wait_time = min(30 * (2 ** self.consecutive_failures), self.max_wait)
                        logging.warning(f"[BING] Received Bing error page, waiting {wait_time} seconds")
                        time_module.sleep(wait_time)
                        continue  # Try next attempt
                    
                    # Check if points were awarded
                    time_module.sleep(3)  # Allow time for points to register
                    pointsAfter = self.browser.utils.getAccountPoints()
                    
                    if pointsAfter > pointsBefore:
                        # Success!
                        self.consecutive_failures = 0
                        logging.debug(f"[BING] Search successful! Points: {pointsBefore} -> {pointsAfter}")
                        return True
                    else:
                        # Wait a bit longer and check again
                        time_module.sleep(randint(5, 10))
                        pointsAfterRetry = self.browser.utils.getAccountPoints()
                        
                        if pointsAfterRetry > pointsBefore:
                            self.consecutive_failures = 0
                            logging.debug(f"[BING] Search successful on retry! Points: {pointsBefore} -> {pointsAfterRetry}")
                            return True
                        
                        logging.debug("[BING] No points awarded for this search")
                
                except Exception as e:
                    logging.error(f"[BING] Error during search attempt {i+1}: {str(e)}")
                    time_module.sleep(randint(5, 15))
            
            # If we get here, all attempts failed
            return False
            
        except Exception as e:
            logging.error(f"[BING] Unhandled exception in bingSearch: {str(e)}")
            return False