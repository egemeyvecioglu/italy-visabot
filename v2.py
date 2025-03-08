import easyocr
import numpy as np
import argparse
import time
import yaml
import logging
import requests
from seleniumbase import SB
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support import expected_conditions as EC
import base64
import io
from PIL import Image

import pytesseract

pytesseract.pytesseract.tesseract_cmd = "/opt/homebrew/bin/tesseract"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_city_config(config_path="config.yaml"):
    """Loads the YAML configuration file and returns its content."""
    try:
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
            logging.info(f"YAML file successfully loaded: {config_path}")
            return config
    except FileNotFoundError:
        logging.error(f"{config_path} not found! Please create the file.")
        raise
    except yaml.YAMLError as e:
        logging.error(f"YAML parsing error: {e}")
        raise


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
        else:
            logging.warning(f"Telegram send error: {response.text}")
    except Exception as e:
        logging.error(f"Telegram API error: {e}")


def extract_six_digit_code(image):
    reader = easyocr.Reader(["en"], gpu=False)

    # Read the image
    if isinstance(image, str):  # image is a file path
        results = reader.readtext(image)
    else:
        # Convert PIL image to numpy array
        img_array = np.array(image)
        results = reader.readtext(img_array)

    # Extract digits
    all_text = "".join([res[1] for res in results])
    digits = "".join([c for c in all_text if c.isdigit()])

    if len(digits) >= 6:
        return digits[:6]
    return None


def check_idata_selenium(city, purpose, city_config, headless):
    """Checks appointment availability for the given city using Selenium with manual intervention."""

    city_data = city_config.get(city + "-" + purpose)
    if not city_data:
        raise ValueError(f"Invalid city selection: {city}")

    telegram_token = city_data["telegram_token"]
    telegram_chat_id = city_data["telegram_chat_id"]

    logging.info(f"Checking appointments for {city.capitalize()} - {purpose}")
    logging.info(f"Telegram notification configured: {telegram_chat_id}")

    url = "https://ita-schengen.idata.com.tr/tr"

    with SB(uc=True, test=True, headless=headless) as sb:

        ########### HOME PAGE ############

        url = "https://ita-schengen.idata.com.tr/tr"
        logging.info(f"Navigating to {url}...")
        sb.activate_cdp_mode(url)
        sb.sleep(3)

        try:
            cloudflare_selector = "/html/body//div[1]/div/div[1]"
            # try to click cloudflare check button if it is visible
            if headless:
                sb.cdp.click_if_visible(cloudflare_selector)
            else:
                sb.cdp.gui_click_element(cloudflare_selector)
        except Exception as e:
            # in case of an exception, it will try to continue but will sleep until next check if it fails
            logging.warning(
                f"Cloudflare button click failed: {e}. Will try to continue, if fails, will sleep until next check."
            )
        sb.sleep(4)

        captcha_image = None
        for try_count in range(3):
            try:
                captcha_image = sb.cdp.get_element_attribute(
                    "/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[1]/img", "src"
                )
                if captcha_image:
                    break
            except Exception as e:
                logging.warning(
                    f"{try_count + 1}/3 - Failed to get captcha image: {e}. Will retry in 2 seconds."
                )
                sb.sleep(2)

        if not captcha_image:
            logging.error("Failed to get captcha image. Exiting...")
            return "Check completed with errors."

        # decode base64 image
        # extract the base64 part after comma
        base64_data = captcha_image.split(",")[1]
        # decode and save as binary data
        captcha_image_data = base64.b64decode(base64_data)
        # load the image with pil
        img = Image.open(io.BytesIO(captcha_image_data))

        # solve verification code
        captcha_code = extract_six_digit_code(img)
        logging.info(f"Extracted captcha code: {captcha_code}")

        # enter the code to the field
        sb.cdp.press_keys(
            "/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[1]/div/div/input",  # captcha input field
            str(captcha_code),
        )

        # click the Randevu Al button
        sb.cdp.click_if_visible("/html/body/div[2]/div[1]/div/div/div/form/div[3]/div[2]/a")
        sb.sleep(4)

        ########### FORM PAGE ############
        sb.cdp.select_option_by_text("#city", city_data["city_value"])
        sb.sleep(2)
        logging.info(f"{city.capitalize()} selected.")

        sb.cdp.select_option_by_text("#office", city_data["office_value"])
        sb.sleep(2)
        logging.info(f"iDATA office selected as {city_data['office_value']}.")

        sb.cdp.select_option_by_text("getapplicationtype", city_data["getapplicationtype"])
        sb.sleep(2)
        logging.info(f"Travel purpose selected as {city_data['getapplicationtype']}.")
        sb.cdp.select_option_by_text("#officetype", "STANDART")
        sb.sleep(2)
        logging.info("Service type selected.")

        for person_count in range(1, 5):
            text = sb.cdp.get_text(
                f"/html/body/div[2]/div/div/div/div[3]/div/form/div/div[1]/div[3]/div[5]/select/option[{person_count + 1}]"
            )

            sb.cdp.select_option_by_text("#totalPerson", text)
            logging.info(f"Person count selected: {person_count}")
            sb.sleep(3)

            result_text = sb.cdp.get_text(
                "/html/body/div[2]/div/div/div/div[3]/div/form/div/div[1]/div[3]/div[7]/div",
            )
            if result_text and "Uygun randevu tarihi bulunmamaktadır" not in result_text:
                logging.info(f"Available appointment found for {person_count} people.")
                send_telegram_message(
                    telegram_token,
                    telegram_chat_id,
                    message=f"{person_count} kişi için uygun randevu:\n\n{result_text}\n\nRandevu almak için:\n {url}",
                )
            else:
                logging.info(f"No available appointment for {person_count} people.")
                logging.info(result_text)

        return "Check completed with errors."


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="iData Appointment Checker")
    parser.add_argument(
        "-c",
        "--city",
        choices=["antalya", "ankara"],
        help="City name (antalya or ankara)",
        default="antalya",
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

    args = parser.parse_args()

    city_config = load_city_config("config.yaml")

    logging.info(f"Starting iData appointment checker for {args.city} - {args.purpose}")
    logging.info("This script requires manual captcha solving.")

    while True:
        try:
            check_idata_selenium(args.city, args.purpose, city_config, args.headless)
            logging.info("Check completed. Waiting 10 minutes before next check...")
            time.sleep(600)  # Wait 10 minutes before the next check
        except KeyboardInterrupt:
            logging.info("Script terminated by user.")
            break
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            logging.error("Waiting 5 minutes before retry...")
            time.sleep(300)  # Wait 5 minutes before retry on error
