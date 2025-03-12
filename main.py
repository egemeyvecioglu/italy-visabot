import easyocr
import numpy as np
import argparse
import time
import yaml
import logging
import requests
from seleniumbase import SB
import base64
import io
from PIL import Image
import gc
import os

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
ACCELERATED_CHECK_INTERVAL = 120  # 2 minutes between checks when available date found
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
    "result_text": "#availableDayInfo",
    "city_select": "#city",
    "office_select": "#office",
    "application_type": "getapplicationtype",
    "office_type": "#officetype",
    "total_person": "#totalPerson",
}


class ConfigManager:
    """Manages configuration loading and access"""

    def __init__(self, config_key, config_path=CONFIG_PATH):
        self.config_path = config_path
        self.config_key = config_key
        self.config = self._load_config()
        self.appointment_config = self._get_appointment_config()

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

    def _get_appointment_config(self):
        """Gets configuration for the specified config key"""
        appointment_config = self.config.get(self.config_key)
        if not appointment_config:
            raise ValueError(f"Invalid configuration key: {self.config_key}")
        return appointment_config


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

    _instance = None

    @classmethod
    def get_instance(cls):
        """Singleton pattern to avoid creating multiple readers"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Initialize EasyOCR reader only once
        self.reader = None

    def _lazy_init_reader(self):
        """Initialize reader only when needed"""
        if self.reader is None:
            self.reader = easyocr.Reader(["en"], gpu=False)

    def extract_six_digit_code(self, image):
        """Extract a 6-digit code from an image using OCR"""
        try:
            self._lazy_init_reader()

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

    def __init__(self, config_key, headless=True):
        self.config_key = config_key
        self.headless = headless
        self.config_manager = ConfigManager(config_key)
        self.appointment_config = self.config_manager.appointment_config
        self.captcha_solver = CaptchaSolver.get_instance()

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
                    logging.info("Captcha image found.")
                    break
            except Exception as e:
                logging.warning(
                    f"{try_count + 1}/{RETRY_COUNT} - Failed to get captcha image: {e}. Retrying..."
                )
                sb.sleep(2)

        if not captcha_image:
            return False

        # Decode base64 image
        try:
            base64_data = captcha_image.split(",")[1]
            captcha_image_data = base64.b64decode(base64_data)
            img = Image.open(io.BytesIO(captcha_image_data))

            # Solve verification code
            captcha_code = self.captcha_solver.extract_six_digit_code(img)

            # Make sure to close the image
            img.close()

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

    def fill_form(self, sb, office_type=None):
        """Fill the appointment form with city data and the specified office type"""
        try:
            # Select city
            sb.cdp.find_element(SELECTORS["city_select"], timeout=30)
            sb.cdp.select_option_by_text(
                SELECTORS["city_select"], self.appointment_config["city_value"]
            )
            sb.sleep(1)
            logging.info(f"City selected as {self.appointment_config['city_value']}.")

            # Select office
            sb.cdp.find_element(SELECTORS["office_select"], timeout=30)
            sb.cdp.select_option_by_text(
                SELECTORS["office_select"], self.appointment_config["office_value"]
            )
            sb.sleep(1)
            logging.info(f"iDATA office selected as {self.appointment_config['office_value']}.")

            # Select application type
            sb.cdp.find_element(SELECTORS["application_type"], timeout=30)
            sb.cdp.select_option_by_text(
                SELECTORS["application_type"], self.appointment_config["getapplicationtype"]
            )
            sb.sleep(1)
            logging.info(
                f"Travel purpose selected as {self.appointment_config['getapplicationtype']}."
            )

            # Select office type if provided
            if office_type:
                sb.cdp.find_element(SELECTORS["office_type"], timeout=30)
                sb.cdp.select_option_by_text(SELECTORS["office_type"], office_type)
                sb.sleep(1)
                logging.info(f"Service type selected as {office_type}.")

            return True
        except Exception as e:
            logging.error(f"Error filling form: {e}")
            return False

    def check_availability(self, sb, office_type):
        """Check appointment availability for different person counts"""
        results = []
        found_available = False

        for person_count in range(1, 5):
            try:
                # Select person count option
                person_option_path = SELECTORS["person_count_option"].format(count=person_count + 1)
                text = sb.cdp.get_text(person_option_path)

                sb.cdp.find_element(SELECTORS["total_person"], timeout=30)
                sb.cdp.select_option_by_text(SELECTORS["total_person"], text)
                logging.info(f"Person count selected: {person_count}")
                sb.sleep(3)  # Wait for results to load

                # Get result text
                result_text = sb.cdp.get_text(SELECTORS["result_text"])

                # Check for availability
                if result_text and "Uygun randevu tarihi bulunmamaktadır" not in result_text:
                    logging.info(
                        f"Available appointment found for {person_count} people at {office_type} office."
                    )

                    # Mark that we found an available slot
                    found_available = True

                    # Send notification
                    NotificationManager.send_telegram_message(
                        self.appointment_config["telegram_token"],
                        self.appointment_config["telegram_chat_id"],
                        message=f"{person_count} kişi için {office_type} ofiste uygun randevu:\n\n{result_text}\n\nRandevu almak için:\n {BASE_URL}",
                    )

                    results.append(
                        {
                            "person_count": person_count,
                            "office_type": office_type,
                            "available": True,
                            "text": result_text,
                        }
                    )
                else:
                    logging.info(
                        f"No available appointment for {person_count} people at {office_type} office."
                    )
                    results.append(
                        {
                            "person_count": person_count,
                            "office_type": office_type,
                            "available": False,
                            "text": result_text if result_text else "No result text",
                        }
                    )
            except Exception as e:
                logging.error(
                    f"Error checking availability for {person_count} people at {office_type} office: {e}"
                )
                results.append(
                    {
                        "person_count": person_count,
                        "office_type": office_type,
                        "available": False,
                        "error": str(e),
                    }
                )

        return results, found_available

    def get_office_types(self):
        """Get list of office types from config"""
        office_types = self.appointment_config.get("office_type")

        # If no office_type in config, use default
        if not office_types:
            return ["STANDART"]

        # If office_type is a string (single office provided), convert to list
        if isinstance(office_types, str):
            return [office_types]

        # If it's already a list, return as is
        return office_types

    def check_appointments(self, sb):
        """Main method to check appointments"""
        logging.info(f"Checking appointments for config: {self.config_key}")
        logging.info(
            f"Telegram notification configured: {self.appointment_config['telegram_chat_id']}"
        )

        office_types = self.get_office_types()
        logging.info(f"Will check appointments for office types: {office_types}")

        all_results = []
        found_available = False

        try:
            # Navigate to homepage and handle Cloudflare
            self.navigate_to_homepage(sb)

            # Solve captcha
            if not self.solve_captcha(sb):
                return "Error solving captcha", "error", False

            logging.info("Going to appointment form page...")

            # Fill the basic form without office type first
            if not self.fill_form(sb):
                return "Error filling form", "error", False

            # Check for each office type
            for office_type in office_types:
                logging.info(f"Checking appointments for office type: {office_type}")

                # Select this office type
                sb.cdp.select_option_by_text(SELECTORS["office_type"], office_type)
                sb.sleep(1)

                # Check availability for this office type
                results, available = self.check_availability(sb, office_type)
                all_results.extend(results)

                # If we found an available slot in any office type, set the flag
                if available:
                    found_available = True

            return (
                f"Check completed successfully for {len(office_types)} office types",
                "success",
                found_available,
            )
        except Exception as e:
            logging.error(f"Exception during appointment check: {e}")
            return f"Error checking appointments: {e}", "error", False


def main():
    """Main function to run the appointment checker"""
    parser = argparse.ArgumentParser(description="iData Appointment Checker")
    parser.add_argument(
        "-config",
        "--config_key",
        type=str,
        required=True,
        help="Configuration key to use (e.g., 'ankara-general')",
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
        "--config_path",
        type=str,
        default=CONFIG_PATH,
        help=f"Path to config file (default: {CONFIG_PATH})",
    )

    args = parser.parse_args()

    logging.info(f"Starting iData appointment checker with config: {args.config_key}")
    logging.info("This script requires OCR for captcha solving.")

    # Variable to track if we're in accelerated mode (checking every 2 minutes)
    accelerated_mode = False

    # Run the main loop
    retry_backoff = 1
    # Create a single SB instance to reuse across iterations.
    with SB(uc=True, test=True, headless=args.headless) as sb:
        while True:
            # Create a new checker instance for each iteration
            checker = AppointmentChecker(args.config_key, args.headless)

            try:
                result, status, found_available = checker.check_appointments(sb)

                # Set the appropriate interval based on availability
                if found_available:
                    # If we found an available appointment, switch to accelerated mode
                    if not accelerated_mode:
                        accelerated_mode = True
                        logging.info(
                            "Available appointment found! Switching to accelerated mode (2-minute intervals)"
                        )

                    check_interval = ACCELERATED_CHECK_INTERVAL
                else:
                    # If no appointments found and we were in accelerated mode, switch back to normal
                    if accelerated_mode:
                        accelerated_mode = False
                        logging.info(
                            "No more available appointments. Switching back to normal mode"
                        )

                    check_interval = args.interval

                if status == "success":
                    if found_available:
                        logging.info(
                            f"{result}. Available appointments found! Waiting {check_interval} seconds before next check..."
                        )
                    else:
                        logging.info(
                            f"{result}. No available appointments. Waiting {check_interval} seconds before next check..."
                        )

                    # Reset backoff on success
                    retry_backoff = 1
                    time.sleep(check_interval)
                else:
                    # Implement exponential backoff (up to a minimum)
                    retry_wait = max(ERROR_RETRY_INTERVAL * retry_backoff, 120)  # min 2 minutes
                    logging.error(f"Error: {result}")
                    logging.error(f"Waiting {retry_wait} seconds before retry...")
                    time.sleep(retry_wait)
                    # Increase backoff for next failure
                    retry_backoff *= 0.8
            except KeyboardInterrupt:
                logging.info("Script terminated by user.")
                break
            except Exception as e:
                logging.error(f"Error: {e}")
                logging.error(f"Waiting {ERROR_RETRY_INTERVAL} seconds before retry...")
                time.sleep(ERROR_RETRY_INTERVAL)

            # Force garbage collection
            del checker
            gc.collect()


if __name__ == "__main__":
    main()
