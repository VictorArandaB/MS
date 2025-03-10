import argparse
import contextlib
import logging
import time
from argparse import Namespace

from pyotp import TOTP
from selenium.common import TimeoutException
from selenium.common.exceptions import (ElementNotInteractableException,
                                        NoSuchElementException)
from selenium.webdriver.common.by import By
from undetected_chromedriver import Chrome

from src.browser import Browser
from src.utils import CONFIG, sendNotification


class Login:
    browser: Browser
    webdriver: Chrome

    def __init__(self, browser: Browser):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.utils = browser.utils

    def check_locked_user(self):
        try:
            element = self.webdriver.find_element(
                By.XPATH, "//div[@id='serviceAbuseLandingTitle']"
            )
            self.locked(element)
        except NoSuchElementException:
            return

    def check_banned_user(self):
        try:
            element = self.webdriver.find_element(By.XPATH, '//*[@id="fraudErrorBody"]')
            self.banned(element)
        except NoSuchElementException:
            return

    def locked(self, element):
        try:
            if element.is_displayed():
                logging.critical("This Account is Locked!")
                self.webdriver.close()
                raise Exception("Account locked, moving to the next account.")
        except (ElementNotInteractableException, NoSuchElementException):
            pass

    def banned(self, element):
        try:
            if element.is_displayed():
                logging.critical("This Account is Banned!")
                self.webdriver.close()
                raise Exception("Account banned, moving to the next account.")
        except (ElementNotInteractableException, NoSuchElementException):
            pass

    def login(self) -> None:
        try:
            if self.utils.isLoggedIn():
                logging.info("[LOGIN] Already logged-in")
                self.check_locked_user()
                self.check_banned_user()
            else:
                logging.info("[LOGIN] Logging-in...")
                self.execute_login()
                logging.info("[LOGIN] Logged-in successfully!")
                self.check_locked_user()
                self.check_banned_user()
            assert self.utils.isLoggedIn()
        except Exception as e:
            logging.error(f"Error during login: {e}")
            self.webdriver.close()
            raise

    def execute_login(self) -> None:
        # Email field
        emailField = self.utils.waitUntilVisible(By.ID, "i0116")
        logging.info("[LOGIN] Entering email...")
        emailField.click()
        emailField.send_keys(self.browser.email)
        assert emailField.get_attribute("value") == self.browser.email
        self.utils.waitUntilClickable(By.ID, "idSIButton9").click()

        # Password-based login, enter password from accounts.json
        passwordField = self.utils.waitUntilClickable(By.NAME, "passwd")
        logging.info("[LOGIN] Entering password...")
        passwordField.click()
        passwordField.send_keys(self.browser.password)
        assert passwordField.get_attribute("value") == self.browser.password
        self.utils.waitUntilClickable(By.ID, "idSIButton9").click()

        # Add a small delay to let the page load
        time.sleep(3)
        
        # Check for TOTP screen using multiple possible element identifiers
        isTOTPEnabled = False
        otpField = None
        
        # Try different possible TOTP input field identifiers
        potential_otp_fields = [
            "idTxtBx_SAOTCC_OTC",  # Original one
            "otc",                 # Possible simplified ID
            "idOTC",               # Another possible ID
            "idTOTP"               # Another possible ID
        ]
        
        # Try to find input fields by type and pattern
        try:
            # Use XPath to find any input field that looks like a TOTP field
            otpField = self.webdriver.find_element(
                By.XPATH, "//input[@type='tel' or @inputmode='numeric' or @pattern='[0-9]*']"
            )
            if otpField:
                isTOTPEnabled = True
                logging.info("[LOGIN] TOTP field detected by input attributes")
        except NoSuchElementException:
            # Try the specific IDs
            for field_id in potential_otp_fields:
                try:
                    otpField = self.webdriver.find_element(By.ID, field_id)
                    isTOTPEnabled = True
                    logging.info(f"[LOGIN] TOTP field detected with ID: {field_id}")
                    break
                except NoSuchElementException:
                    continue
        
        # If still not found, check if page contains OTP-related text
        if not isTOTPEnabled:
            page_source = self.webdriver.page_source.lower()
            otp_indicators = [
                "one-time code", 
                "verification code",
                "security code",
                "authenticator",
                "two-factor",
                "2fa",
                "otp"
            ]
            for indicator in otp_indicators:
                if indicator in page_source:
                    logging.info(f"[LOGIN] TOTP likely required (detected text: '{indicator}')")
                    
                    
                    # Try to find any input field
                    try:
                        otpField = self.webdriver.find_element(By.XPATH, "//input")
                        isTOTPEnabled = True
                        logging.info("[LOGIN] Found potential TOTP field")
                        break
                    except NoSuchElementException:
                        continue
        
        if isTOTPEnabled and otpField:
            # One-time password required
            if self.browser.totp is not None:
                # TOTP token provided
                logging.info("[LOGIN] Entering OTP...")
                otp = TOTP(self.browser.totp.replace(" ", "")).now()
                
                # Clear the field first (sometimes necessary)
                otpField.clear()
                # Send the OTP code
                otpField.send_keys(otp)
                logging.info(f"[LOGIN] Entered OTP code: {otp}")

                
                # Find and click the submit button
                submit_buttons = [
                    "idSubmit_SAOTCC_Continue",
                    "idSubmit_Continue",
                    "idSIButton9"
                ]
                
                submit_clicked = False
                for button_id in submit_buttons:
                    try:
                        submit_button = self.webdriver.find_element(By.ID, button_id)
                        submit_button.click()
                        logging.info(f"[LOGIN] Clicked OTP submit button with ID: {button_id}")
                        submit_clicked = True
                        break
                    except NoSuchElementException:
                        continue
                
                # If no specific button found, try any button
                if not submit_clicked:
                    try:
                        submit_button = self.webdriver.find_element(By.XPATH, "//button[@type='submit']")
                        submit_button.click()
                        logging.info("[LOGIN] Clicked generic submit button")
                        submit_clicked = True
                    except NoSuchElementException:
                        logging.warning("[LOGIN] Could not find OTP submit button")
                
                # Wait for the next page
                time.sleep(5)