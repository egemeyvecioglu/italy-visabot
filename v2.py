import easyocr
import numpy as np
import argparse
import time
import yaml
import logging
import requests
from seleniumbase import SB
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import base64
import io
from PIL import Image
from functools import lru_cache
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Constants
CONFIG_PATH = "config.yaml"
BASE_URL = "https://ita-schengen.idata.com.tr/tr"
WAIT_TIME = 10  # Maximum wait time in seconds for elements
RETRY_COUNT = 3
CHECK_INTERVAL = 600  # 10 minutes between checks
ERROR_RETRY_INTERVAL = 300  # 5 minutes retry on error

# XPath selectors
SELECTORS = {
    "cloudflare": "/html/body//div[1]/div/div[1]",
    "captcha_img": "/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[1]/img",
    "captcha_input": "/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[1]/div/div/input",
    "submit_button": "/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[2]/a",
    "person_count_option": (
        "/html/body/div[2]/div/div/div/div[3]/div/form/div/div[1]/div[3]/div[5]/select/option[{count}]"
    ),
    "result_text": "/html/body/div[2]/div/div/div/div[3]/div/form/div/div[1]/div[3]/div[7]/div",
    "city_select": "#city",
    "office_select": "#office",
    "application_type": "getapplicationtype",
    "office_type": "#officetype",
    "total_person": "#totalPerson",
}

# Form values
FORM_VALUES = {"office_type": "STANDART"}


class ConfigManager:
    """Manages configuration loading and access"""

    def __init__(self, appointment_config, config_path=CONFIG_PATH):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self):
        """Loads the YAML configuration file and returns its content."""
        try:
            with open(self.config_path, "r") as file:
                config = yaml.safe_load(file)
                logging.info(f"YAML file successfully loaded: {self.config_path}")
                return config
        except FileNotFoundError:
            logging.error(f"{self.config_path} not found! Please create the file.")
            raise
        except yaml.YAMLError as e:
            logging.error(f"YAML parsing error: {e}")
            raise

    def get_city_config(self, city, purpose):
        """Gets configuration for a specific city and purpose"""
        config_key = f"{city}-{purpose}"
        city_data = self.config.get(config_key)
        if not city_data:
            raise ValueError(f"Invalid city-purpose combination: {config_key}")
        return city_data


class NotificationManager:
    """Manages sending notifications"""

    @staticmethod
    def send_telegram_message(token, chat_id, message):
        """Sends a message to a Telegram bot."""
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
        }
        try:
            response = requests.post(url, data=payload)
            if response.status_code == 200:
                logging.info("Telegram notification sent successfully.")
                return True
            else:
                logging.warning(f"Telegram send error: {response.text}")
                return False
        except Exception as e:
            logging.error(f"Telegram API error: {e}")
            return False


class CaptchaSolver:
    """Handles captcha recognition"""

    def __init__(self):
        # Initialize EasyOCR reader only once
        self.reader = easyocr.Reader(["en"], gpu=False)

    def extract_six_digit_code(self, image):
        """Extract a 6-digit code from an image using OCR"""
        try:
            # Read the image
            if isinstance(image, str):  # image is a file path
                results = self.reader.readtext(image)
            else:
                # Convert PIL image to numpy array
                img_array = np.array(image)
                results = self.reader.readtext(img_array)

            # Extract digits
            all_text = "".join([res[1] for res in results])
            digits = "".join([c for c in all_text if c.isdigit()])

            if len(digits) >= 6:
                return digits[:6]
            return None
        except Exception as e:
            logging.error(f"Error extracting captcha: {e}")
            return None


class AppointmentChecker:
    """Main class that checks for appointment availability"""

    def __init__(self, city, purpose, headless=True):
        self.city = city
        self.purpose = purpose
        self.headless = headless
        self.config_manager = ConfigManager()
        self.city_config = self.config_manager.get_city_config(city, purpose)
        self.captcha_solver = CaptchaSolver()

    @lru_cache(maxsize=10)
    def _get_result_cache_key(self, person_count):
        """Create a cache key for results"""
        return f"{self.city}-{self.purpose}-{person_count}"

    def navigate_to_homepage(self, sb):
        """Navigate to the homepage and handle Cloudflare"""
        logging.info(f"Navigating to {BASE_URL}...")
        sb.activate_cdp_mode(BASE_URL)

        # Wait for page to load
        sb.sleep(3)  # Short initial wait

        try:
            # Handle Cloudflare check if present
            if self.headless:
                sb.cdp.click_if_visible(SELECTORS["cloudflare"])
            else:
                sb.cdp.gui_click_element(SELECTORS["cloudflare"])
        except Exception as e:
            logging.warning(f"Cloudflare handling: {e}. Continuing...")

        # Wait for page to stabilize
        sb.sleep(2)

    def solve_captcha(self, sb):
        """Solve the captcha and submit the form"""
        captcha_image = None

        # Try to get the captcha image with retries
        for try_count in range(RETRY_COUNT):
            try:
                captcha_image = sb.cdp.get_element_attribute(SELECTORS["captcha_img"], "src")
                if captcha_image:
                    break
            except Exception as e:
                logging.warning(
                    f"{try_count + 1}/{RETRY_COUNT} - Failed to get captcha image: {e}. Retrying..."
                )
                sb.sleep(2)

        if not captcha_image:
            raise Exception("Failed to get captcha image after multiple attempts")

        # Decode base64 image
        try:
            base64_data = captcha_image.split(",")[1]
            captcha_image_data = base64.b64decode(base64_data)
            img = Image.open(io.BytesIO(captcha_image_data))

            # Solve verification code
            captcha_code = self.captcha_solver.extract_six_digit_code(img)

            if not captcha_code:
                raise Exception("Could not extract a valid captcha code")

            logging.info(f"Extracted captcha code: {captcha_code}")

            # Enter the code to the field
            sb.cdp.press_keys(
                SELECTORS["captcha_input"],
                str(captcha_code),
            )

            # Click the submit button
            sb.cdp.click_if_visible(SELECTORS["submit_button"])
            sb.sleep(3)  # Wait for form page to load
            return True
        except Exception as e:
            logging.error(f"Captcha solving error: {e}")
            return False

    def fill_form(self, sb):
        """Fill the appointment form with city data"""
        try:
            # Select city
            sb.cdp.select_option_by_text(SELECTORS["city_select"], self.city_config["city_value"])
            sb.sleep(1)
            logging.info(f"{self.city.capitalize()} selected.")

            # Select office
            sb.cdp.select_option_by_text(
                SELECTORS["office_select"], self.city_config["office_value"]
            )
            sb.sleep(1)
            logging.info(f"iDATA office selected as {self.city_config['office_value']}.")

            # Select application type
            sb.cdp.select_option_by_text(
                SELECTORS["application_type"], self.city_config["getapplicationtype"]
            )
            sb.sleep(1)
            logging.info(f"Travel purpose selected as {self.city_config['getapplicationtype']}.")

            # Select office type
            sb.cdp.select_option_by_text(SELECTORS["office_type"], FORM_VALUES["office_type"])
            sb.sleep(1)
            logging.info("Service type selected.")
            return True
        except Exception as e:
            logging.error(f"Error filling form: {e}")
            return False

    def check_availability(self, sb):
        """Check appointment availability for different person counts"""
        results = []

        for person_count in range(1, 5):
            try:
                # Select person count option
                person_option_path = SELECTORS["person_count_option"].format(count=person_count + 1)
                text = sb.cdp.get_text(person_option_path)

                sb.cdp.select_option_by_text(SELECTORS["total_person"], text)
                logging.info(f"Person count selected: {person_count}")
                sb.sleep(2)  # Wait for results to load

                # Get result text
                result_text = sb.cdp.get_text(SELECTORS["result_text"])

                # Check for availability
                if result_text and "Uygun randevu tarihi bulunmamaktadır" not in result_text:
                    logging.info(f"Available appointment found for {person_count} people.")

                    # Send notification
                    NotificationManager.send_telegram_message(
                        self.city_config["telegram_token"],
                        self.city_config["telegram_chat_id"],
                        message=f"{person_count} kişi için uygun randevu:\n\n{result_text}\n\nRandevu almak için:\n {BASE_URL}",
                    )

                    results.append(
                        {"person_count": person_count, "available": True, "text": result_text}
                    )
                else:
                    logging.info(f"No available appointment for {person_count} people.")
                    results.append(
                        {
                            "person_count": person_count,
                            "available": False,
                            "text": result_text if result_text else "No result text",
                        }
                    )
            except Exception as e:
                logging.error(f"Error checking availability for {person_count} people: {e}")
                results.append({"person_count": person_count, "available": False, "error": str(e)})

        return results

    def check_appointments(self):
        """Main method to check appointments"""
        logging.info(f"Checking appointments for {self.city.capitalize()} - {self.purpose}")
        logging.info(f"Telegram notification configured: {self.city_config['telegram_chat_id']}")

        try:
            with SB(uc=True, test=True, headless=self.headless) as sb:
                # Navigate to homepage and handle Cloudflare
                self.navigate_to_homepage(sb)

                # Solve captcha
                if not self.solve_captcha(sb):
                    return "Captcha solving failed"

                # Fill form
                if not self.fill_form(sb):
                    return "Form filling failed"

                # Check availability
                results = self.check_availability(sb)

                return "Check completed successfully"
        except Exception as e:
            logging.error(f"Appointment check failed: {e}")
            return f"Check failed: {str(e)}"


def main():
    """Main function to run the appointment checker"""
    parser = argparse.ArgumentParser(description="iData Appointment Checker")
    parser.add_argument(
        "-c",
        "--city",
        choices=["antalya", "ankara"],
        help="City name (antalya or ankara)",
        default="ankara",
    )
    parser.add_argument(
        "-p",
        "--purpose",
        type=str,
        choices=["general", "education"],
        default="general",
        help="Purpose of travel (only general and education are supported for now)",
    )
    parser.add_argument(
        "--headless",
        type=bool,
        default=True,
        help="Run the script in headless mode",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CHECK_INTERVAL,
        help="Interval between checks in seconds (default: 600)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=CONFIG_PATH,
        help=f"Path to config file (default: {CONFIG_PATH})",
    )

    args = parser.parse_args()

    logging.info(f"Starting iData appointment checker for {args.city} - {args.purpose}")
    logging.info("This script requires OCR for captcha solving.")

    # Create the appointment checker
    checker = AppointmentChecker(args.city, args.purpose, args.headless)

    # Run the main loop
    retry_backoff = 1
    while True:
        try:
            result = checker.check_appointments()
            logging.info(f"{result}. Waiting {args.interval} seconds before next check...")
            # Reset backoff on success
            retry_backoff = 1
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logging.info("Script terminated by user.")
            break
        except Exception as e:
            # Implement exponential backoff (up to a maximum)
            retry_wait = min(ERROR_RETRY_INTERVAL * retry_backoff, 1800)  # Max 30 minutes
            logging.error(f"Unexpected error: {e}")
            logging.error(f"Waiting {retry_wait} seconds before retry...")
            time.sleep(retry_wait)
            # Increase backoff for next failure
            retry_backoff *= 2


if __name__ == "__main__":
    main()
