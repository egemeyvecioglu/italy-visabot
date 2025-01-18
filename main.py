import traceback
import requests
import logging
import time
import argparse
import yaml
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

logging.basicConfig(
    level=logging.DEBUG,
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


def check_idata_selenium(city, purpose, city_config):
    """Checks appointment availability for the given city using Selenium."""
    try:
        city_data = city_config.get(city + "-" + purpose)
        if not city_data:
            raise ValueError(f"Invalid city selection: {city}")

        telegram_token = city_data["telegram_token"]
        telegram_chat_id = city_data["telegram_chat_id"]

        logging.info(f"Telegram token: {telegram_token}")
        logging.info(f"Telegram chat id: {telegram_chat_id}")

        driver = Driver(uc=True, headless=True)
        url = "https://ita-schengen.idata.com.tr/tr/appointment-form"
        logging.info(f"Navigating to {url} using SeleniumBase...")
        driver.uc_open_with_reconnect(url, reconnect_time=6)

        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )

        try:
            logging.info("Checking CAPTCHA...")
            driver.uc_gui_click_captcha()
        except Exception as e:
            logging.info(f"CAPTCHA check failed: {e}")

        # Select residence city
        residence_city = Select(
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "city")))
        )
        residence_city.select_by_value(city_data["city_value"])
        logging.info(f"{city.capitalize()} selected.")

        # Select iDATA office
        idata_office = Select(
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "office")))
        )
        idata_office.select_by_value(city_data["office_value"])
        logging.info(f"iDATA office selected as {city_data['office_value']}.")

        # Select travel purpose
        travel_purpose = Select(
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "getapplicationtype"))
            )
        )
        travel_purpose.select_by_value(city_data["getapplicationtype"])
        logging.info(f"Travel purpose selected as {city_data['getapplicationtype']}.")

        # Select service type
        service_type = Select(
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "officetype")))
        )
        service_type.select_by_value("1")
        logging.info("Service type selected.")

        # Loop through person counts
        for person_count in range(1, 5):
            person_count_select = Select(
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "totalPerson"))
                )
            )
            person_count_select.select_by_value(str(person_count))
            logging.info(f"Person count selected: {person_count}")

            time.sleep(3)

            # Check results
            result_div = WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located((By.ID, "availableDayInfo"))
            )
            result_text = result_div.text

            if result_text and "Uygun randevu tarihi bulunmamaktadır" not in result_text:
                logging.info(f"Available appointment found for {person_count} people.")

                homepage_url = "https://ita-schengen.idata.com.tr/tr"
                send_telegram_message(
                    telegram_token,
                    telegram_chat_id,
                    message=f"{person_count} kişi için uygun randevu:\n\n{result_text}\n\nRandevu almak için:\n {homepage_url}",
                )
            else:
                print("*" * 50)
                logging.info(f"No available appointment for {person_count} people.")
                logging.info(result_text)
                print("*" * 50)

        return "Check completed."

    except Exception as e:
        logging.error(f"An error occurred: {e}")
        logging.error(traceback.format_exc())
        return None

    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Appointment Check Bot")
    parser.add_argument(
        "-c",
        "--city",
        required=True,
        choices=["antalya", "ankara"],
        help="City name (antalya or ankara)",
    )
    parser.add_argument(
        "-p",
        "--purpose",
        type=str,
        choices=["general", "education"],
        default="general",
        help="Purpose of travel (only general and education are supported for now)",
    )
    args = parser.parse_args()

    city_config = load_city_config("config.yaml")

    while True:
        check_idata_selenium(args.city, args.purpose, city_config)
        time.sleep(600)
