#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Megafon Parser Bot — единый файл.
Объединяет парсер (MegafonCaptchaParser) и Telegram-бота для управления им.
"""

import asyncio
import threading
import logging
import os
import re
import tempfile
import json
import random
import string
import base64
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple, Any

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, CallbackQuery
from aiogram.utils.markdown import hbold, hcode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from playwright.async_api import async_playwright, Page, Response

# ===================== КОНФИГУРАЦИЯ =====================
BOT_TOKEN = "8596447755:AAFq7yCgIlOcnr2W7t1ANu1jdvX9e0fIfXs"
ADMIN_IDS = [8133517773]  # замените на реальные ID администраторов
ACTIVATION_DATA_FILE = "activation_data.json"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ===================== УПРАВЛЕНИЕ АКТИВАЦИЯМИ =====================
class ActivationManager:
    """Управление ключами активации и подписками пользователей"""
    def __init__(self, data_file: str = ACTIVATION_DATA_FILE):
        self.data_file = data_file
        self.activations: Dict[str, dict] = {}
        self.load()

    def _generate_key(self, length: int = 16) -> str:
        chars = string.ascii_uppercase + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    def load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    self.activations = json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки активаций: {e}")
                self.activations = {}
        else:
            self.activations = {}

    def save(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.activations, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения активаций: {e}")

    def create_key(self, admin_id: int, days: int, target_user_id: Optional[int] = None) -> str:
        key = self._generate_key()
        created_at = datetime.now().isoformat()
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        self.activations[key] = {
            "key": key,
            "created_by": admin_id,
            "created_at": created_at,
            "expiry": expiry,
            "days": days,
            "status": "active",       # active, used, expired, deactivated
            "activated_by": None,
            "activated_at": None,
            "subscription_expiry": None,
            "target_user_id": target_user_id
        }
        self.save()
        return key

    def activate_key(self, key: str, user_id: int) -> Tuple[bool, Optional[str]]:
        key = key.strip().upper()
        if key not in self.activations:
            return False, "❌ Ключ не найден."

        key_data = self.activations[key]

        if key_data["status"] == "deactivated":
            return False, "❌ Ключ деактивирован администратором."

        target_id = key_data.get("target_user_id")
        if target_id is not None and target_id != user_id:
            return False, f"❌ Этот ключ предназначен для пользователя с ID {target_id}. Вы не можете его активировать."

        if key_data["status"] != "active":
            return False, "❌ Ключ уже использован или недействителен."

        expiry = datetime.fromisoformat(key_data["expiry"])
        if expiry < datetime.now():
            key_data["status"] = "expired"
            self.save()
            return False, "❌ Срок действия ключа истёк."

        key_data["status"] = "used"
        key_data["activated_by"] = user_id
        key_data["activated_at"] = datetime.now().isoformat()
        sub_expiry = datetime.now() + timedelta(days=key_data["days"])
        key_data["subscription_expiry"] = sub_expiry.isoformat()
        self.save()

        return True, sub_expiry.isoformat()

    def deactivate_key(self, key: str) -> Tuple[bool, Optional[int], str]:
        """
        Деактивирует ключ.
        Возвращает (успех, user_id если ключ был активирован, сообщение)
        """
        key = key.strip().upper()
        if key not in self.activations:
            return False, None, "❌ Ключ не найден."

        key_data = self.activations[key]

        if key_data["status"] == "deactivated":
            return False, None, "❌ Ключ уже деактивирован."

        user_id = key_data.get("activated_by")
        key_data["status"] = "deactivated"
        self.save()

        if user_id:
            return True, user_id, f"✅ Ключ деактивирован. Пользователь {user_id} лишён подписки."
        else:
            return True, None, "✅ Ключ деактивирован (не был активирован)."

    def is_subscription_active(self, user_id: int) -> bool:
        for key_data in self.activations.values():
            if key_data.get("activated_by") == user_id:
                if key_data.get("status") == "deactivated":
                    return False
                sub_expiry = key_data.get("subscription_expiry")
                if sub_expiry:
                    expiry_date = datetime.fromisoformat(sub_expiry)
                    return expiry_date > datetime.now()
        return False

    def get_user_subscription_expiry(self, user_id: int) -> Optional[datetime]:
        for key_data in self.activations.values():
            if key_data.get("activated_by") == user_id and key_data.get("status") != "deactivated":
                sub_expiry = key_data.get("subscription_expiry")
                if sub_expiry:
                    return datetime.fromisoformat(sub_expiry)
        return None

    def get_all_keys(self) -> Dict[str, dict]:
        return self.activations


# ===================== ПРОКСИ МЕНЕДЖЕР =====================
class ProxyManager:
    """Управление прокси – копия логики из оригинального MegafonParserGUI"""
    def __init__(self):
        self.proxies: List[str] = []
        self.bad_proxies: List[str] = []
        self.proxy_file: Optional[str] = None

    @staticmethod
    def normalize_proxy_format(proxy_str: str) -> str:
        proxy_str = proxy_str.strip()
        if proxy_str.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            return proxy_str

        if any(x in proxy_str.lower() for x in ['логин:', 'пароль:', 'login:', 'password:']):
            parts = proxy_str.split()
            address = parts[0]
            login = password = None
            for part in parts:
                if ':' in part:
                    key, val = part.split(':', 1)
                    key_low = key.lower()
                    if key_low in ('логин', 'login'):
                        login = val.strip()
                    elif key_low in ('пароль', 'password'):
                        password = val.strip()
            if login and password:
                return f"http://{login}:{password}@{address}"
            elif login:
                return f"http://{login}@{address}"
            return f"http://{address}"
        else:
            if '://' not in proxy_str:
                return f"http://{proxy_str}"
            return proxy_str

    @staticmethod
    def parse_proxy_string(proxy_str: str) -> Optional[Dict]:
        try:
            if '@' in proxy_str:
                auth_part, server_part = proxy_str.split('@', 1)
                if '://' in auth_part:
                    protocol, auth_credentials = auth_part.split('://', 1)
                else:
                    protocol = 'http'
                    auth_credentials = auth_part
                if ':' in auth_credentials:
                    username, password = auth_credentials.split(':', 1)
                else:
                    username = auth_credentials
                    password = ''
                return {
                    'server': f'{protocol}://{server_part}',
                    'username': username,
                    'password': password
                }
            else:
                if '://' in proxy_str:
                    return {'server': proxy_str}
                else:
                    return {'server': f'http://{proxy_str}'}
        except Exception as e:
            logger.error(f"Ошибка парсинга прокси {proxy_str}: {e}")
            return None

    async def test_proxy_async(self, proxy_str: str) -> bool:
        try:
            proxy_config = self.parse_proxy_string(proxy_str)
            if not proxy_config:
                return False

            async with async_playwright() as p:
                launch_opts = {
                    'headless': True,
                    'args': ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
                    'timeout': 15000
                }
                if proxy_config.get('server'):
                    launch_opts['proxy'] = {
                        'server': proxy_config['server']
                    }
                    if proxy_config.get('username') and proxy_config.get('password'):
                        launch_opts['proxy']['username'] = proxy_config['username']
                        launch_opts['proxy']['password'] = proxy_config['password']

                browser = await p.chromium.launch(**launch_opts)
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                try:
                    resp = await page.goto("http://httpbin.org/ip", timeout=15000, wait_until='domcontentloaded')
                    if resp and resp.status == 200:
                        return True
                except:
                    return False
                finally:
                    await browser.close()
            return False
        except Exception:
            return False

    def load_proxies(self, filepath: str, mode: str = "replace") -> int:
        new_proxies = []
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            norm = self.normalize_proxy_format(line)
                            new_proxies.append(norm)
                if mode == "replace":
                    self.proxies = new_proxies
                elif mode == "append":
                    existing = set(self.proxies)
                    for p in new_proxies:
                        if p not in existing:
                            self.proxies.append(p)
                self.proxy_file = filepath
                return len(self.proxies)
            else:
                return 0
        except Exception as e:
            logger.error(f"Ошибка загрузки прокси: {e}")
            return 0

    async def test_proxies(self, progress_callback=None):
        working = []
        bad = []
        total = len(self.proxies)
        for idx, proxy in enumerate(self.proxies):
            ok = await self.test_proxy_async(proxy)
            if ok:
                working.append(proxy)
            else:
                bad.append(proxy)
            if progress_callback:
                await progress_callback(idx+1, total, proxy, ok)
        self.proxies = working
        self.bad_proxies = bad
        return working, bad

    def remove_bad_proxies(self) -> Tuple[int, int]:
        if not self.proxy_file or not os.path.exists(self.proxy_file):
            return 0, 0

        try:
            with open(self.proxy_file, 'r', encoding='utf-8') as f:
                all_lines = f.readlines()

            good_lines = []
            removed = 0
            bad_normalized = [self.normalize_proxy_format(bp) for bp in self.bad_proxies]

            for line in all_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('#'):
                    norm = self.normalize_proxy_format(stripped)
                    if norm in bad_normalized:
                        removed += 1
                        continue
                good_lines.append(line)

            with open(self.proxy_file, 'w', encoding='utf-8') as f:
                f.writelines(good_lines)

            self.proxies = [self.normalize_proxy_format(l.strip()) for l in good_lines
                           if l.strip() and not l.strip().startswith('#')]
            self.bad_proxies = []
            return removed, len(self.proxies)
        except Exception as e:
            logger.error(f"Ошибка удаления нерабочих прокси: {e}")
            return 0, 0


# ===================== ОСНОВНОЙ ПАРСЕР (из megafon_parser_core.py, без GUI) =====================
class MegafonCaptchaParser:
    def __init__(self, rucaptcha_api_key: str = None, proxy_config: Optional[Dict] = None, region: str = "moscow", 
                 thread_id: int = 1, gui_callback=None, base_folder: str = "megafon_parser_results", 
                 shared_numbers: set = None, shared_duplicates: dict = None, digits_count: int = 4):
        self.region = region
        self.thread_id = thread_id
        self.gui_callback = gui_callback  # может быть None для бота
        self.base_folder = base_folder
        self.digits_count = digits_count
        
        # Общие структуры данных между потоками
        self.shared_numbers = shared_numbers if shared_numbers is not None else set()
        self.shared_duplicates = shared_duplicates if shared_duplicates is not None else {}
        
        # Инициализация атрибутов до вызова методов
        self.logger = None
        self.found_numbers = set()
        self.captcha_solved = 0
        self.captcha_failed = 0
        self.current_captcha_image = None
        self.captcha_detected = False
        self.duplicates_count = 0
        self.max_captcha_retries = 3
        self.proxy_config = proxy_config
        self.is_running = False
        self.error_count = 0
        self.successful_searches = 0
        self.last_search_time = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.current_combination = None
        
        # Настройки задержек для предотвращения капчи
        self.delay_settings = {
            'min_delay': 10,  # Минимальная задержка между запросами (секунды)
            'max_delay': 30,  # Максимальная задержка между запросами
            'session_delay': 60,  # Задержка между сессиями
            'request_count': 0,  # Счетчик запросов
            'last_request_time': None,
            'requests_per_hour': 50  # Максимальное количество запросов в час
        }
        
        # Сначала создаем базовые папки
        self.create_base_folders()
        
        # Затем настраиваем логирование
        self.setup_logging()
        
        # Инициализируем URL
        self.base_url = f"https://{region}.shop.megafon.ru"
        self.main_url = f"https://{region}.shop.megafon.ru/connect/chnumber/lnumber/free"
        self.free_url = self.main_url
        self.api_url = f"https://{region}.shop.megafon.ru/api/msisdn/msisdn"
        self.output_file = os.path.join(self.results_folder, f"megafonnum_{region}_{thread_id}.txt")
        self.rucaptcha_api_key = rucaptcha_api_key
        
        # Генератор комбинаций
        self.combinations = self.generate_combinations()
        
        # Статистика
        self.stats = {
            'numbers_found': 0,
            'requests_made': 0,
            'errors': 0,
            'captchas_solved': 0,
            'page_reloads': 0,
            'successful_searches': 0,
            'last_activity': datetime.now().strftime("%H:%M:%S"),
            'status': 'Остановлен',
            'duplicates': 0,
            'combinations_tried': 0,
            'current_combination': None,
            'delay_multiplier': 1.0,
            'digits_count': digits_count
        }
        
        self.log_message(f"Парсер инициализирован для региона: {region}, поиск по {digits_count} цифрам")

    def generate_combinations(self):
        """Генерирует все возможные комбинации заданной длины"""
        combinations = []
        max_number = 10 ** self.digits_count - 1
        for i in range(max_number + 1):
            combinations.append(f"{i:0{self.digits_count}d}")
        random.shuffle(combinations)  # Перемешиваем для случайного порядка
        return combinations

    def get_next_combination(self):
        """Возвращает следующую комбинацию"""
        if not self.combinations:
            self.combinations = self.generate_combinations()  # Перезапускаем если закончились
        
        self.current_combination = self.combinations.pop(0)
        self.stats['combinations_tried'] += 1
        self.stats['current_combination'] = self.current_combination
        return self.current_combination

    def create_base_folders(self):
        """Создает базовую структуру папок"""
        folders = [
            self.base_folder,
            os.path.join(self.base_folder, "logs", "bad"),
            os.path.join(self.base_folder, "logs", "good"),
            os.path.join(self.base_folder, "captchas"),
            os.path.join(self.base_folder, "results"),
            os.path.join(self.base_folder, "screenshots"),
            os.path.join(self.base_folder, "debug")
        ]
        
        for folder in folders:
            os.makedirs(folder, exist_ok=True)
            
        self.logs_folder = os.path.join(self.base_folder, "logs")
        self.logs_bad_folder = os.path.join(self.logs_folder, "bad")
        self.logs_good_folder = os.path.join(self.logs_folder, "good")
        self.captchas_folder = os.path.join(self.base_folder, "captchas")
        self.results_folder = os.path.join(self.base_folder, "results")
        self.screenshots_folder = os.path.join(self.base_folder, "screenshots")
        self.debug_folder = os.path.join(self.base_folder, "debug")

    def setup_logging(self):
        """Настраивает логирование"""
        try:
            main_log_file = os.path.join(self.logs_good_folder, f"parser_thread_{self.thread_id}.log")
            error_log_file = os.path.join(self.logs_bad_folder, f"errors_thread_{self.thread_id}.log")
            
            self.logger = logging.getLogger(f"parser_{self.thread_id}")
            self.logger.setLevel(logging.INFO)
            
            for handler in self.logger.handlers[:]:
                self.logger.removeHandler(handler)
            
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            
            file_handler = logging.FileHandler(main_log_file, encoding='utf-8')
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(formatter)
            
            error_handler = logging.FileHandler(error_log_file, encoding='utf-8')
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(formatter)
            
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            
            self.logger.addHandler(file_handler)
            self.logger.addHandler(error_handler)
            self.logger.addHandler(console_handler)
            
            self.logger.info(f"Логирование настроено для потока {self.thread_id}")
            
        except Exception as e:
            print(f"Ошибка настройки логирования: {e}")

    def log_message(self, message: str, level: str = "INFO"):
        """Логирование с callback (для совместимости)"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] [Thread-{self.thread_id}] {message}"
        
        if self.logger:
            if level == "ERROR":
                self.logger.error(formatted_message)
            elif level == "WARNING":
                self.logger.warning(formatted_message)
            else:
                self.logger.info(formatted_message)
        else:
            print(formatted_message)
            
        if self.gui_callback:
            self.gui_callback(f"[Thread-{self.thread_id}] {message}", level)

    async def apply_anti_captcha_delay(self):
        """Применяет задержку для предотвращения капчи"""
        try:
            now = datetime.now()
            
            if not self.delay_settings['last_request_time']:
                self.delay_settings['last_request_time'] = now
                return
            
            time_since_last = (now - self.delay_settings['last_request_time']).total_seconds()
            
            if time_since_last < self.delay_settings['min_delay']:
                wait_time = self.delay_settings['min_delay'] - time_since_last
                wait_time *= self.stats['delay_multiplier']
                self.log_message(f"⏳ Задержка для предотвращения капчи: {wait_time:.1f} сек")
                await asyncio.sleep(wait_time)
            
            if self.captcha_detected:
                self.stats['delay_multiplier'] = min(self.stats['delay_multiplier'] * 1.5, 5.0)
                self.log_message(f"📈 Увеличиваем задержку до {self.stats['delay_multiplier']:.1f}x")
            elif self.stats['delay_multiplier'] > 1.0:
                self.stats['delay_multiplier'] = max(self.stats['delay_multiplier'] * 0.9, 1.0)
            
            self.delay_settings['request_count'] += 1
            self.delay_settings['last_request_time'] = datetime.now()
            
            if self.delay_settings['request_count'] % 10 == 0:
                long_delay = random.uniform(30, 60)
                self.log_message(f"⏳ Большая пауза после 10 запросов: {long_delay:.1f} сек")
                await asyncio.sleep(long_delay)
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в задержке: {e}", "WARNING")

    async def solve_captcha(self, captcha_image_base64: str) -> Optional[str]:
        if not self.rucaptcha_api_key:
            self.log_message("❌ API ключ RuCaptcha не указан", "ERROR")
            return None
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                if not captcha_image_base64 or len(captcha_image_base64) < 100:
                    self.log_message("❌ Получена пустая или слишком короткая base64 строка", "ERROR")
                    return None
                
                if "base64," in captcha_image_base64:
                    captcha_image_base64 = captcha_image_base64.split("base64,")[1]
                
                captcha_image_base64 = captcha_image_base64.strip()
                captcha_image_base64 = captcha_image_base64.replace('\n', '').replace('\r', '').replace(' ', '')
                
                try:
                    decoded = base64.b64decode(captcha_image_base64, validate=True)
                    if len(decoded) < 100:
                        self.log_message("❌ Изображение слишком маленькое", "ERROR")
                        return None
                except Exception as e:
                    self.log_message(f"❌ Невалидный base64 формат: {e}", "ERROR")
                    return None
                
                url = "http://rucaptcha.com/in.php"
                form_data = aiohttp.FormData()
                form_data.add_field('key', self.rucaptcha_api_key)
                form_data.add_field('method', 'base64')
                form_data.add_field('body', captcha_image_base64)
                form_data.add_field('json', '1')
                form_data.add_field('phrase', '0')
                form_data.add_field('numeric', '0')
                form_data.add_field('min_len', '0')
                form_data.add_field('max_len', '0')
                form_data.add_field('language', '2')
                form_data.add_field('lang', 'ru')
                
                self.log_message(f"📤 Попытка {attempt + 1}/{max_attempts}: Отправка капчи на решение...")
                
                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(url, data=form_data, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            result_text = await response.text()
                            self.log_message(f"📥 Ответ от RuCaptcha: {result_text}")
                            
                            try:
                                result = json.loads(result_text)
                            except json.JSONDecodeError:
                                self.log_message(f"❌ Не удалось разобрать ответ JSON: {result_text}", "ERROR")
                                if attempt < max_attempts - 1:
                                    await asyncio.sleep(2)
                                    continue
                                return None
                            
                            if result.get('status') == 1:
                                captcha_id = result['request']
                                self.log_message(f"✅ Капча отправлена на решение, ID: {captcha_id}")
                                return await self.wait_for_captcha_solution(session, captcha_id)
                            else:
                                error_text = result.get('error_text', 'Неизвестная ошибка')
                                self.log_message(f"❌ Ошибка отправки капчи: {error_text}", "ERROR")
                                
                                if "ERROR_UPLOAD" in error_text and attempt < max_attempts - 1:
                                    self.log_message(f"🔄 Повторная попытка через 3 секунды...")
                                    await asyncio.sleep(3)
                                    continue
                                
                                return None
                                
                    except aiohttp.ClientError as e:
                        self.log_message(f"❌ Сетевая ошибка при отправке капчи: {e}", "ERROR")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(3)
                            continue
                        return None
                    except asyncio.TimeoutError:
                        self.log_message("❌ Таймаут при отправке капчи", "ERROR")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(3)
                            continue
                        return None
                        
            except Exception as e:
                self.log_message(f"❌ Непредвиденная ошибка при решении капчи: {e}", "ERROR")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(3)
                continue
        
        return None

    async def wait_for_captcha_solution(self, session: aiohttp.ClientSession, captcha_id: str) -> Optional[str]:
        try:
            max_wait_time = 120
            start_time = time.time()
            check_interval = 3
            
            while time.time() - start_time < max_wait_time:
                await asyncio.sleep(check_interval)
                
                result_url = f"http://rucaptcha.com/res.php?key={self.rucaptcha_api_key}&action=get&id={captcha_id}&json=1"
                
                try:
                    async with session.get(result_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        result_text = await resp.text()
                        
                        try:
                            result = json.loads(result_text)
                        except json.JSONDecodeError:
                            self.log_message(f"❌ Не удалось разобрать ответ получения решения: {result_text}", "ERROR")
                            continue
                        
                        if result.get('status') == 1:
                            captcha_text = result['request']
                            self.log_message(f"✅ Капча решена: {captcha_text}")
                            self.captcha_solved += 1
                            self.stats['captchas_solved'] = self.captcha_solved
                            return captcha_text
                        elif result.get('request') == 'CAPCHA_NOT_READY':
                            continue
                        else:
                            error_text = result.get('request', 'Неизвестная ошибка')
                            self.log_message(f"❌ Ошибка решения капчи: {error_text}", "ERROR")
                            self.captcha_failed += 1
                            return None
                            
                except Exception as e:
                    self.log_message(f"⚠️ Ошибка при получении решения: {e}", "WARNING")
                    continue
            
            self.log_message("❌ Таймаут ожидания решения капчи", "ERROR")
            self.captcha_failed += 1
            return None
            
        except Exception as e:
            self.log_message(f"❌ Ошибка ожидания решения капчи: {e}", "ERROR")
            self.captcha_failed += 1
            return None

    async def submit_captcha_solution(self, page: Page, captcha_text: str) -> bool:
        try:
            self.log_message(f"📤 Отправляем решение капчи: {captcha_text}")
            await asyncio.sleep(1)
            
            # Стратегия 1
            if await self._submit_captcha_strategy_input_button(page, captcha_text):
                return True
            # Стратегия 2
            if await self._submit_captcha_strategy_javascript(page, captcha_text):
                return True
            # Стратегия 3
            if await self._submit_captcha_strategy_api(page, captcha_text):
                return True
            
            self.log_message("❌ Не удалось отправить решение капчи ни одним способом", "ERROR")
            return False
            
        except Exception as e:
            self.log_message(f"❌ Ошибка при отправке решения капчи: {e}", "ERROR")
            return False

    async def _submit_captcha_strategy_input_button(self, page: Page, captcha_text: str) -> bool:
        try:
            input_selectors = [
                'input[name="captcha"]', 'input[name="captcha_code"]', 'input[name="code"]',
                'input[placeholder*="капч"]', 'input[placeholder*="код"]', '#captcha',
                '.captcha-input', 'input#captcha', 'input[type="text"][name*="captcha"]',
                'input.text-input[name*="captcha"]'
            ]
            captcha_input = None
            for selector in input_selectors:
                try:
                    captcha_input = await page.wait_for_selector(selector, timeout=3000, state='visible')
                    if captcha_input:
                        self.log_message(f"✅ Нашли поле ввода капчи: {selector}")
                        break
                except:
                    continue
            
            if not captcha_input:
                frames = page.frames
                for frame in frames:
                    for selector in input_selectors:
                        try:
                            captcha_input = await frame.wait_for_selector(selector, timeout=2000, state='visible')
                            if captcha_input:
                                self.log_message(f"✅ Нашли поле ввода капчи во фрейме: {selector}")
                                break
                        except:
                            continue
                    if captcha_input:
                        break
            
            if not captcha_input:
                return False
            
            await captcha_input.fill('')
            await captcha_input.type(captcha_text, delay=100)
            await asyncio.sleep(0.5)
            
            button_selectors = [
                'button[type="submit"]', 'input[type="submit"]', 'button:has-text("Отправить")',
                'button:has-text("Проверить")', 'button:has-text("Подтвердить")', 'button:has-text("OK")',
                'button:has-text("Далее")', '.submit-button', '.captcha-submit',
                'button.btn-primary', 'button.btn-success', 'button.btn-submit'
            ]
            submit_button = None
            for selector in button_selectors:
                try:
                    submit_button = await page.wait_for_selector(selector, timeout=2000, state='visible')
                    if submit_button:
                        self.log_message(f"✅ Нашли кнопку отправки: {selector}")
                        break
                except:
                    continue
            
            if submit_button:
                await submit_button.click()
                await asyncio.sleep(2)
                return True
            else:
                await captcha_input.press('Enter')
                await asyncio.sleep(2)
                return True
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в стратегии 1: {e}", "WARNING")
            return False

    async def _submit_captcha_strategy_javascript(self, page: Page, captcha_text: str) -> bool:
        try:
            result = await page.evaluate("""
                (captchaText) => {
                    try {
                        const inputs = document.querySelectorAll('input, textarea');
                        let captchaField = null;
                        for (let input of inputs) {
                            const name = (input.name || '').toLowerCase();
                            const id = (input.id || '').toLowerCase();
                            const placeholder = (input.placeholder || '').toLowerCase();
                            if (name.includes('captcha') || id.includes('captcha') || 
                                placeholder.includes('капч') || placeholder.includes('код')) {
                                captchaField = input;
                                break;
                            }
                        }
                        if (!captchaField) {
                            console.log('Поле капчи не найдено');
                            return false;
                        }
                        captchaField.value = captchaText;
                        let form = captchaField.closest('form');
                        if (!form) {
                            const forms = document.querySelectorAll('form');
                            if (forms.length > 0) {
                                form = forms[0];
                            }
                        }
                        if (form) {
                            form.submit();
                            console.log('Форма отправлена через JS');
                            return true;
                        } else {
                            const submitButtons = document.querySelectorAll(
                                'button[type="submit"], input[type="submit"]'
                            );
                            if (submitButtons.length > 0) {
                                submitButtons[0].click();
                                console.log('Клик по кнопке через JS');
                                return true;
                            }
                        }
                        return false;
                    } catch (error) {
                        console.error('Ошибка JS:', error);
                        return false;
                    }
                }
            """, captcha_text)
            
            if result:
                self.log_message("✅ Решение капчи отправлено через JavaScript")
                await asyncio.sleep(2)
                return True
            return False
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в стратегии 2: {e}", "WARNING")
            return False

    async def _submit_captcha_strategy_api(self, page: Page, captcha_text: str) -> bool:
        try:
            current_url = page.url
            if '/api/' in current_url or 'ajax' in current_url.lower():
                csrf_token = await page.evaluate("""
                    () => {
                        return document.querySelector('meta[name="csrf-token"]')?.content || 
                               document.querySelector('input[name="_token"]')?.value ||
                               document.querySelector('input[name="csrf_token"]')?.value;
                    }
                """)
                
                if csrf_token:
                    form_data = {'captcha': captcha_text, '_token': csrf_token}
                    await page.evaluate("""
                        async (formData) => {
                            try {
                                const response = await fetch(window.location.href, {
                                    method: 'POST',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'X-Requested-With': 'XMLHttpRequest',
                                        'X-CSRF-TOKEN': formData._token
                                    },
                                    body: JSON.stringify({captcha: formData.captcha})
                                });
                                return response.ok;
                            } catch (error) {
                                console.error('API ошибка:', error);
                                return false;
                            }
                        }
                    """, form_data)
                    
                    self.log_message("✅ Решение капчи отправлено через API")
                    await asyncio.sleep(2)
                    return True
            return False
            
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в стратегии 3: {e}", "WARNING")
            return False

    async def download_captcha_image(self, page: Page, url: str) -> Optional[str]:
        try:
            if url.startswith('/'):
                url = f"{self.base_url}{url}"
            elif url.startswith('./'):
                url = f"{self.base_url}{url[1:]}"
            elif not url.startswith(('http://', 'https://', 'data:')):
                url = f"{self.base_url}/{url}"
            
            self.log_message(f"📥 Загружаем изображение: {url[:100]}...")
            
            # Через fetch
            try:
                image_data = await page.evaluate("""
                    async (url) => {
                        try {
                            const response = await fetch(url);
                            if (!response.ok) throw new Error(`HTTP ${response.status}`);
                            const blob = await response.blob();
                            return new Promise((resolve, reject) => {
                                const reader = new FileReader();
                                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                                reader.onerror = reject;
                                reader.readAsDataURL(blob);
                            });
                        } catch (error) {
                            console.error('Fetch error:', error);
                            return null;
                        }
                    }
                """, url)
                if image_data:
                    self.log_message(f"✅ Изображение загружено через fetch, длина: {len(image_data)}")
                    return image_data
            except Exception as e:
                self.log_message(f"⚠️ Ошибка загрузки через fetch: {e}", "WARNING")
            
            # Через aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                        if response.status == 200:
                            image_bytes = await response.read()
                            image_base64 = base64.b64encode(image_bytes).decode('utf-8')
                            self.log_message(f"✅ Изображение загружено через aiohttp, длина: {len(image_base64)}")
                            return image_base64
                        else:
                            self.log_message(f"❌ Ошибка HTTP {response.status} при загрузке изображения", "WARNING")
            except Exception as e:
                self.log_message(f"⚠️ Ошибка загрузки через aiohttp: {e}", "WARNING")
            
            return None
            
        except Exception as e:
            self.log_message(f"❌ Ошибка загрузки изображения капчи: {e}", "ERROR")
            return None

    async def handle_captcha_on_page(self, page: Page) -> bool:
        try:
            self.log_message("🔄 Проверяем наличие капчи на странице...")
            captcha_detected = False
            captcha_base64 = None
            
            # Поиск по тексту
            page_content = await page.content()
            captcha_keywords = ['капч', 'captcha', 'код безопасности', 'введите код', 'защита от роботов']
            for keyword in captcha_keywords:
                if keyword.lower() in page_content.lower():
                    self.log_message(f"🔍 Обнаружено ключевое слово капчи: {keyword}")
                    captcha_detected = True
                    break
            
            # Поиск по селекторам
            if not captcha_detected:
                captcha_selectors = [
                    'img[src*="captcha"]', 'img[src*="Captcha"]', 'img[src*="CAPTCHA"]', 'img.captcha',
                    '.captcha img', '#captcha-image', '.captcha-image', 'img[alt*="капч"]', 'img[alt*="код"]',
                    'img[title*="капч"]', 'img[alt*="captcha"]', 'img[title*="captcha"]', 'img#captcha',
                    '[class*="captcha"] img', '[id*="captcha"] img'
                ]
                for selector in captcha_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            self.log_message(f"✅ Нашли элемент капчи по селектору: {selector}")
                            captcha_detected = True
                            src = await element.get_attribute('src')
                            if src:
                                if src.startswith('data:image'):
                                    try:
                                        captcha_base64 = src.split('base64,')[1]
                                        self.log_message(f"📊 Длина base64: {len(captcha_base64)}")
                                        break
                                    except:
                                        captcha_base64 = src
                                else:
                                    self.log_message(f"📥 Загружаем по URL: {src[:100]}...")
                                    captcha_base64 = await self.download_captcha_image(page, src)
                            if not captcha_base64:
                                data_src = await element.get_attribute('data-src')
                                if data_src:
                                    self.log_message(f"📥 Нашли data-src: {data_src[:100]}...")
                                    captcha_base64 = await self.download_captcha_image(page, data_src)
                    except Exception as e:
                        self.log_message(f"⚠️ Ошибка при проверке селектора {selector}: {e}", "WARNING")
                        continue
            
            # Скрытые поля
            if not captcha_base64:
                hidden_selectors = [
                    'input[type="hidden"][name*="captcha"]', 'input[type="hidden"][value*="base64"]',
                    'input[name="captcha_image"]', '[data-captcha]', 'input#captcha_image',
                    'textarea[name="captcha"]', '.captcha-data', '[data-image]'
                ]
                for selector in hidden_selectors:
                    try:
                        hidden_input = await page.query_selector(selector)
                        if hidden_input:
                            value = await hidden_input.get_attribute('value') or await hidden_input.text_content()
                            if value:
                                self.log_message(f"🔍 Нашли скрытое поле капчи: {selector}")
                                if 'base64,' in value:
                                    try:
                                        captcha_base64 = value.split('base64,')[1]
                                        self.log_message(f"📊 Длина из скрытого поля: {len(captcha_base64)}")
                                        captcha_detected = True
                                        break
                                    except:
                                        pass
                                elif 'data:image' in value:
                                    captcha_base64 = value
                                    captcha_detected = True
                                    break
                    except Exception as e:
                        self.log_message(f"⚠️ Ошибка проверки скрытого поля {selector}: {e}", "WARNING")
                        continue
            
            # Скриншот области
            if captcha_detected and not captcha_base64:
                self.log_message("📸 Делаем скриншот области капчи...")
                container_selectors = [
                    '.captcha', '#captcha', '.captcha-container', '[class*="captcha"]', '[id*="captcha"]',
                    '.form-group:has(img[src*="captcha"])', '.row:has(img[src*="captcha"])'
                ]
                for selector in container_selectors:
                    container = await page.query_selector(selector)
                    if container:
                        screenshot_bytes = await container.screenshot()
                        if screenshot_bytes:
                            captcha_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                            self.log_message(f"📸 Сделали скриншот капчи, длина base64: {len(captcha_base64)}")
                            break
            
            if captcha_detected and captcha_base64:
                self.log_message("🎯 Капча обнаружена, пытаемся решить...")
                # Сохраняем для отладки
                try:
                    captcha_filename = os.path.join(self.captchas_folder, f"captcha_{int(time.time())}_{self.thread_id}.png")
                    if 'base64,' in captcha_base64:
                        captcha_base64 = captcha_base64.split('base64,')[1]
                    captcha_base64 = captcha_base64.strip().replace('\n', '').replace('\r', '').replace(' ', '')
                    with open(captcha_filename, "wb") as f:
                        f.write(base64.b64decode(captcha_base64))
                    file_size = os.path.getsize(captcha_filename)
                    self.log_message(f"💾 Капча сохранена: {captcha_filename} ({file_size} байт)")
                except Exception as e:
                    self.log_message(f"⚠️ Ошибка сохранения капчи: {e}", "WARNING")
                
                captcha_text = await self.solve_captcha(captcha_base64)
                if captcha_text:
                    result = await self.submit_captcha_solution(page, captcha_text)
                    if result:
                        self.log_message("✅ Капча успешно решена и отправлена")
                        return True
                    else:
                        self.log_message("❌ Не удалось отправить решение капчи", "ERROR")
                        return False
                else:
                    self.log_message("❌ Не удалось решить капчу", "ERROR")
                    return False
            
            elif captcha_detected and not captcha_base64:
                self.log_message("⚠️ Капча обнаружена, но не удалось получить изображение", "WARNING")
                return False
            else:
                self.log_message("ℹ️ Капча не обнаружена на странице")
                return True
                
        except Exception as e:
            self.log_message(f"❌ Ошибка при обработке капчи на странице: {e}", "ERROR")
            return False

    async def wait_for_page_ready(self, page: Page, timeout: int = 30000):
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=timeout)
            await page.wait_for_load_state('networkidle', timeout=timeout)
            self.log_message("Страница загружена")
            return True
        except Exception as e:
            self.log_message(f"Ошибка загрузки страницы: {e}", "WARNING")
            return False

    async def navigate_to_main_page(self, page: Page):
        try:
            self.log_message(f"Переходим на основную страницу: {self.main_url}")
            await self.apply_anti_captcha_delay()
            
            strategies = [
                self._try_direct_navigation,
                self._try_with_referer,
                self._try_via_homepage,
                self._try_with_retry
            ]
            
            for strategy in strategies:
                if await strategy(page):
                    if await self.handle_captcha_on_page(page):
                        return True
            return False
            
        except Exception as e:
            self.log_message(f"Ошибка перехода на основную страницу: {e}", "ERROR")
            return False

    async def _try_direct_navigation(self, page: Page):
        try:
            response = await page.goto(self.main_url, wait_until='domcontentloaded', timeout=30000)
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
        except Exception as e:
            self.log_message(f"Прямой переход не удался: {e}", "WARNING")
            return False

    async def _try_with_referer(self, page: Page):
        try:
            await page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(2)
            response = await page.goto(self.main_url, wait_until='domcontentloaded', timeout=30000, referer=self.base_url)
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
        except Exception as e:
            self.log_message(f"Переход с реферером не удался: {e}", "WARNING")
            return False

    async def _try_via_homepage(self, page: Page):
        try:
            response = await page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
            if not response or response.status != 200:
                return False
            await asyncio.sleep(2)
            link_selectors = [
                'a[href*="chnumber"]', 'a[href*="lnumber"]', 'a:has-text("Выбрать номер")',
                'a:has-text("Подобрать номер")'
            ]
            for selector in link_selectors:
                link = await page.query_selector(selector)
                if link:
                    self.log_message(f"Нашли ссылку на выбор номера: {selector}")
                    await link.click()
                    await asyncio.sleep(3)
                    current_url = page.url
                    if self.main_url in current_url or 'lnumber' in current_url:
                        await self.wait_for_page_ready(page)
                        return True
                    break
            return False
        except Exception as e:
            self.log_message(f"Переход через домашнюю страницу не удался: {e}", "WARNING")
            return False

    async def _try_with_retry(self, page: Page):
        try:
            await asyncio.sleep(5)
            response = await page.goto(self.main_url, wait_until='domcontentloaded', timeout=40000)
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
        except Exception as e:
            self.log_message(f"Повторный переход не удался: {e}", "WARNING")
            return False

    async def enter_combination_and_search_numbers(self, page: Page, combination: str) -> bool:
        try:
            self.log_message(f"🔍 Ищем номера по комбинации: {combination}")
            
            if not await self.handle_captcha_on_page(page):
                self.log_message("❌ Не удалось обойти капчу перед вводом комбинации", "ERROR")
                return False
            
            await self.apply_anti_captcha_delay()
            
            input_selectors = [
                '#favoriteNumber', 'input[name="favoriteNumber"]', 'input[type="tel"]', 'input.search-input',
                '.phone-input', 'input[placeholder*="номер"]', 'input[placeholder*="цифр"]',
                f'input[placeholder*="{self.digits_count} цифр"]', f'input[placeholder*="{self.digits_count} цифры"]',
                'input#number_search'
            ]
            
            input_field = None
            for selector in input_selectors:
                try:
                    input_field = await page.wait_for_selector(selector, timeout=5000)
                    if input_field:
                        self.log_message(f"Нашли поле ввода по селектору: {selector}")
                        break
                except:
                    continue
            
            if not input_field:
                self.log_message("Не найдено поле для ввода номера", "WARNING")
                screenshot_path = os.path.join(self.screenshots_folder, f"debug_{int(time.time())}_{self.thread_id}.png")
                await page.screenshot(path=screenshot_path)
                self.log_message(f"📸 Сделан скриншот для отладки: {screenshot_path}")
                return False
            
            await input_field.fill('')
            await input_field.type(combination, delay=random.uniform(50, 150))
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            search_selectors = [
                'button[type="submit"]', 'input[type="submit"]', 'button.search-button', '.search-btn',
                'button:has-text("Найти")', 'button:has-text("Поиск")', 'button:has-text("Поиск номера")',
                'button:has-text("🔍")', 'button:has-text("Поиск по цифрам")', 'button.btn-primary', 'button.btn-search'
            ]
            
            search_button = None
            for selector in search_selectors:
                try:
                    search_button = await page.wait_for_selector(selector, timeout=3000)
                    if search_button:
                        self.log_message(f"Нашли кнопку поиска по селектору: {selector}")
                        break
                except:
                    continue
            
            if not search_button:
                self.log_message("Кнопка поиска не найдена, пробуем Enter...")
                await input_field.press('Enter')
            else:
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await search_button.click()
            
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except:
                self.log_message("⚠️ Таймаут ожидания загрузки результатов", "WARNING")
            
            await asyncio.sleep(3)
            
            result_selectors = [
                '.phone-numbers', '.numbers-list', '.numbers-grid', '.msisdn-list', '[data-type="phone"]',
                '.number-item', '.phone-item', '.phone-number', '.phone-number-item',
                '.catalog-numbers__item', '.catalog-item'
            ]
            
            has_results = False
            for selector in result_selectors:
                results = await page.query_selector_all(selector)
                if results and len(results) > 0:
                    self.log_message(f"Нашли {len(results)} номеров по селектору: {selector}")
                    has_results = True
                    await self.extract_numbers_from_page(page, combination)
                    break
            
            if not has_results:
                content = await page.content()
                phone_patterns = [r'\+7\d{10}', r'7\d{10}', r'8\d{10}', r'\d{11}']
                for pattern in phone_patterns:
                    phones = re.findall(pattern, content)
                    if phones:
                        self.log_message(f"Нашли {len(phones)} номеров по паттерну")
                        for phone in phones:
                            if phone.startswith('+7'):
                                phone_str = phone[2:]
                            elif phone.startswith('7') or phone.startswith('8'):
                                phone_str = phone[1:]
                            else:
                                phone_str = phone
                            if len(phone_str) == 10:
                                self.process_found_number(phone_str, combination)
                        break
            
            await self.handle_captcha_on_page(page)
            return True
            
        except Exception as e:
            self.log_message(f"❌ Ошибка при поиске номеров: {e}", "ERROR")
            return False

    async def extract_numbers_from_page(self, page: Page, combination: str):
        try:
            number_elements = await page.query_selector_all(
                '.phone-number, .number, .msisdn, [data-phone], [data-number], .catalog-numbers__phone'
            )
            for element in number_elements[:100]:
                text = await element.text_content()
                if text:
                    phone_match = re.search(r'\d{10,11}', text)
                    if phone_match:
                        phone_str = phone_match.group()
                        if len(phone_str) == 11:
                            if phone_str.startswith('7') or phone_str.startswith('8'):
                                phone_str = phone_str[1:]
                        if len(phone_str) == 10:
                            self.process_found_number(phone_str, combination)
            
            phone_elements = await page.query_selector_all('[data-phone], [data-number], [data-msisdn]')
            for element in phone_elements:
                phone_attr = (await element.get_attribute('data-phone') or
                             await element.get_attribute('data-number') or
                             await element.get_attribute('data-msisdn'))
                if phone_attr:
                    phone_str = re.sub(r'\D', '', phone_attr)
                    if len(phone_str) == 10:
                        self.process_found_number(phone_str, combination)
        except Exception as e:
            self.log_message(f"⚠️ Ошибка извлечения номеров: {e}", "WARNING")

    def process_found_number(self, phone_str: str, combination: str):
        if phone_str in self.shared_numbers:
            self.duplicates_count += 1
            self.stats['duplicates'] = self.duplicates_count
            if phone_str in self.shared_duplicates:
                self.shared_duplicates[phone_str] += 1
            else:
                self.shared_duplicates[phone_str] = 1
        else:
            self.shared_numbers.add(phone_str)
            self.found_numbers.add(phone_str)
            self.stats['numbers_found'] = len(self.found_numbers)
            self.log_message(f"🎉 Найден номер: +7{phone_str} (комбинация: {combination})")

    async def setup_browser(self):
        if not self.is_running:
            return None, None, None, None

        max_retries = 5
        for attempt in range(max_retries):
            try:
                self.playwright = await async_playwright().start()

                args = [
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--window-size=1366,768',
                    '--disable-gpu',
                    '--disable-software-rasterizer',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--disable-site-isolation-trials',
                    '--disable-features=BlockInsecurePrivateNetworkRequests',
                    '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ]
                args.extend([
                    '--disable-blink-features=AutomationControlled',
                    '--excludeSwitches=enable-automation',
                    '--disable-features=site-per-process',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-default-apps',
                    '--disable-popup-blocking',
                    '--disable-translate',
                    '--disable-extensions',
                    '--disable-background-networking',
                    '--disable-sync',
                    '--metrics-recording-only',
                    '--disable-client-side-phishing-detection',
                    '--disable-component-update',
                    '--disable-domain-reliability',
                    '--disable-breakpad',
                    '--disable-cloud-import',
                    '--disable-app-list-dismiss-on-blur',
                    '--disable-datasaver-prompt',
                    '--disable-hang-monitor',
                    '--disable-ipc-flooding-protection',
                    '--disable-prompt-on-repost',
                    '--disable-back-forward-cache'
                ])

                launch_options = {'headless': True, 'args': args, 'timeout': 60000}

                if self.proxy_config and self.proxy_config.get('server'):
                    proxy_server = self.proxy_config['server']
                    if proxy_server.startswith('socks4://'):
                        proxy_type = 'socks4'
                        proxy_server_clean = proxy_server.replace('socks4://', '')
                    elif proxy_server.startswith('socks5://'):
                        proxy_type = 'socks5'
                        proxy_server_clean = proxy_server.replace('socks5://', '')
                    elif proxy_server.startswith('http://'):
                        proxy_type = 'http'
                        proxy_server_clean = proxy_server.replace('http://', '')
                    elif proxy_server.startswith('https://'):
                        proxy_type = 'https'
                        proxy_server_clean = proxy_server.replace('https://', '')
                    else:
                        proxy_type = 'http'
                        proxy_server_clean = proxy_server

                    proxy_for_playwright = {'server': proxy_server}
                    if self.proxy_config.get('username') and self.proxy_config.get('password'):
                        proxy_for_playwright['username'] = self.proxy_config['username']
                        proxy_for_playwright['password'] = self.proxy_config['password']
                    launch_options['proxy'] = proxy_for_playwright

                    if proxy_type in ['socks4', 'socks5']:
                        launch_options['args'].extend([
                            f'--proxy-server={proxy_server_clean}',
                            '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost'
                        ])

                    self.log_message(f"🔄 Используется прокси: {proxy_server} (тип: {proxy_type})")
                else:
                    self.log_message("⚠️ Работаем без прокси")

                self.browser = await self.playwright.chromium.launch(**launch_options)

                context_options = {
                    'viewport': {'width': 1366, 'height': 768},
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'java_script_enabled': True,
                    'ignore_https_errors': True,
                    'bypass_csp': True,
                    'extra_http_headers': {
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Cache-Control': 'max-age=0'
                    }
                }

                if self.proxy_config and self.proxy_config.get('server'):
                    context_options['proxy'] = launch_options['proxy']

                self.context = await self.browser.new_context(**context_options)

                await self.context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: { isInstalled: false } };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                    window.__chromedriver = false;
                """)

                self.page = await self.context.new_page()
                self.page.set_default_timeout(60000)
                self.page.set_default_navigation_timeout(60000)

                self.log_message("✅ Браузер успешно запущен")
                return self.playwright, self.browser, self.context, self.page

            except Exception as e:
                error_msg = str(e)
                self.log_message(f"❌ Попытка {attempt + 1}/{max_retries} запуска браузера не удалась: {error_msg}", "ERROR")
                await self.cleanup_browser()
                if any(proxy_error in error_msg.lower() for proxy_error in ['proxy', 'tunnel', 'connection failed', 'timeout', 'connection closed', 'err_connection_closed']):
                    self.log_message("⚠️ Проблема с прокси, пробуем без прокси...", "WARNING")
                    self.proxy_config = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    self.log_message("❌ Не удалось запустить браузер после нескольких попыток", "ERROR")
                    return None, None, None, None

        return None, None, None, None

    async def handle_api_response(self, response: Response):
        if self.api_url in response.url:
            try:
                data = await response.json()
                self.stats['requests_made'] += 1
                self.stats['last_activity'] = datetime.now().strftime("%H:%M:%S")

                if data.get('success') is False and data.get('errors'):
                    for error in data.get('errors', []):
                        if any(keyword in error.get('message', '').lower() for keyword in ['капч', 'captcha', 'код', 'code']):
                            self.log_message("Обнаружена капча")
                            captcha_image = data.get('payload', {}).get('captcha', '')
                            if captcha_image:
                                self.captcha_detected = True
                                self.current_captcha_image = captcha_image
                                captcha_text = await self.solve_captcha(captcha_image)
                                if captcha_text and self.page:
                                    await self.submit_api_captcha_solution(captcha_text)
                            return

                await self.parse_numbers_from_api(data)

            except Exception as e:
                self.log_message(f"Ошибка обработки API: {e}", "ERROR")
                self.stats['errors'] += 1

    async def parse_numbers_from_api(self, data: Dict[str, Any]):
        try:
            numbers_found = False
            if 'regular' in data and isinstance(data['regular'], dict):
                numbers_data = data['regular'].get('numbers', [])
                if isinstance(numbers_data, list):
                    for number_group in numbers_data:
                        if isinstance(number_group, dict) and 'phones' in number_group:
                            phones = number_group.get('phones', [])
                            for phone in phones:
                                phone_str = str(phone)
                                if len(phone_str) >= 10:
                                    if len(phone_str) > 10:
                                        phone_str = phone_str[-10:]
                                    self.process_found_number(phone_str, self.current_combination)
                                    numbers_found = True
            elif 'numbers' in data and isinstance(data['numbers'], list):
                for number_item in data['numbers']:
                    if isinstance(number_item, dict) and 'phone' in number_item:
                        phone_str = str(number_item['phone'])
                        if len(phone_str) >= 10:
                            if len(phone_str) > 10:
                                phone_str = phone_str[-10:]
                            self.process_found_number(phone_str, self.current_combination)
                            numbers_found = True
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and 'phone' in item:
                        phone_str = str(item['phone'])
                        if len(phone_str) >= 10:
                            if len(phone_str) > 10:
                                phone_str = phone_str[-10:]
                            self.process_found_number(phone_str, self.current_combination)
                            numbers_found = True
            if numbers_found:
                self.log_message(f"📊 Найдено номеров в API: {self.stats['numbers_found']}")
        except Exception as e:
            self.log_message(f"⚠️ Ошибка парсинга API: {e}", "WARNING")

    async def submit_api_captcha_solution(self, captcha_text: str):
        try:
            captcha_url = f"{self.api_url}/captcha"
            csrf_token = await self.page.evaluate('''() => {
                return document.querySelector('meta[name="csrf-token"]')?.content || 
                       document.querySelector('input[name="_token"]')?.value;
            }''')
            headers = {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            if csrf_token:
                headers['X-CSRF-TOKEN'] = csrf_token
            payload = {'captcha': captcha_text, 'favoriteNumber': self.current_combination}
            async with aiohttp.ClientSession() as session:
                async with session.post(captcha_url, json=payload, headers=headers) as response:
                    if response.status == 200:
                        self.log_message("✅ Решение капчи отправлено")
                        self.captcha_detected = False
                        return True
                    else:
                        self.log_message(f"❌ Ошибка отправки решения капчи: {response.status}", "ERROR")
                        return False
        except Exception as e:
            self.log_message(f"❌ Ошибка при отправке решения капчи через API: {e}", "ERROR")
            return False

    async def perform_search_cycle(self, page: Page):
        try:
            combination = self.get_next_combination()
            if not combination:
                self.log_message("Комбинации закончились", "WARNING")
                return False

            self.log_message(f"🔍 Начинаем обработку комбинации: {combination}")
            
            if not await self.navigate_to_main_page(page):
                self.log_message("❌ Не удалось перейти на главную страницу", "ERROR")
                return False

            if not await self.enter_combination_and_search_numbers(page, combination):
                self.log_message(f"❌ Не удалось обработать комбинацию {combination}", "WARNING")
                return False

            self.log_message(f"✅ Успешно выполнили поиск для комбинации {combination}")
            self.stats['successful_searches'] += 1
            return True

        except Exception as e:
            self.log_message(f"❌ Ошибка в цикле поиска: {e}", "ERROR")
            self.stats['errors'] += 1
            return False

    async def run_parser_cycle(self):
        self.stats['status'] = 'Запущен'

        while self.is_running:
            try:
                self.log_message(f"🚀 Начало нового цикла")
                self.log_message(f"📍 Регион: {self.region}")
                self.log_message(f"📊 Номеров: {len(self.found_numbers)}, Общих: {len(self.shared_numbers)}")
                self.log_message(f"🔢 Комбинаций обработано: {self.stats['combinations_tried']}")
                self.log_message(f"🔢 Поиск по {self.digits_count} цифрам")

                playwright, browser, context, page = await self.setup_browser()
                if not all([playwright, browser, context, page]):
                    self.log_message("❌ Не удалось запустить браузер", "ERROR")
                    await asyncio.sleep(10)
                    continue

                page.on('response', self.handle_api_response)

                try:
                    search_count = 0
                    while self.is_running and search_count < 10:
                        search_count += 1
                        if not await self.perform_search_cycle(page):
                            self.log_message("🔄 Пробуем следующую комбинацию...")
                            continue
                        await self.apply_anti_captcha_delay()
                        extra_delay = random.uniform(2, 5)
                        await asyncio.sleep(extra_delay)

                except Exception as e:
                    self.log_message(f"❌ Ошибка в сессии: {e}", "ERROR")
                    self.stats['errors'] += 1

                finally:
                    await self.cleanup_browser()

                if self.is_running:
                    session_delay = random.uniform(self.delay_settings['session_delay'],
                                                  self.delay_settings['session_delay'] * 1.5)
                    self.log_message(f"⏳ Большая пауза между сессиями: {session_delay:.1f} сек")
                    await asyncio.sleep(session_delay)

            except Exception as e:
                if self.is_running:
                    self.log_message(f"💥 Критическая ошибка: {e}", "ERROR")
                    self.stats['errors'] += 1
                    await asyncio.sleep(30)

        self.stats['status'] = 'Остановлен'
        self.log_message("🛑 Парсер завершил работу")

    async def cleanup_browser(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            self.log_message("🧹 Ресурсы очищены")
        except Exception as e:
            self.log_message(f"⚠️ Ошибка очистки: {e}", "WARNING")

    def start(self):
        self.is_running = True
        self.stats['status'] = 'Запускается'
        self.log_message("🚀 Запуск парсера...")
        try:
            asyncio.run(self.run_parser_cycle())
        except RuntimeError as e:
            if "Event loop is closed" not in str(e):
                raise
        except Exception as e:
            self.log_message(f"❌ Критическая ошибка при запуске: {e}", "ERROR")

    def stop(self):
        self.is_running = False
        self.stats['status'] = 'Останавливается'
        self.log_message("🛑 Получена команда остановки...")
        self.log_message("✅ Парсер остановлен")

    def get_stats(self) -> Dict:
        return {
            'thread_id': self.thread_id,
            'region': self.region,
            'numbers_found': len(self.found_numbers),
            'captchas_solved': self.captcha_solved,
            'captchas_failed': self.captcha_failed,
            'duplicates': self.duplicates_count,
            'errors': self.stats['errors'],
            'requests_made': self.stats['requests_made'],
            'successful_searches': self.stats['successful_searches'],
            'last_activity': self.stats['last_activity'],
            'status': self.stats['status'],
            'is_running': self.is_running,
            'shared_numbers_total': len(self.shared_numbers),
            'combinations_tried': self.stats['combinations_tried'],
            'current_combination': self.stats['current_combination'],
            'delay_multiplier': self.stats['delay_multiplier'],
            'digits_count': self.digits_count
        }


# ===================== УПРАВЛЕНИЕ ДАННЫМИ ПОЛЬЗОВАТЕЛЯ =====================
class UserData:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.api_key: Optional[str] = None
        self.proxies: List[str] = []
        self.bad_proxies: List[str] = []
        self.proxy_file: Optional[str] = None
        self.settings = {
            'min_delay': 10,
            'max_delay': 30,
            'session_delay': 60
        }
        self.history: List[dict] = []
        self.current_session: Optional[dict] = None
        self.is_active: bool = False
        self.expiry_date: Optional[str] = None

    @property
    def data_path(self) -> str:
        return f"user_data/user_{self.user_id}.json"

    def save(self):
        os.makedirs("user_data", exist_ok=True)
        data = {
            'api_key': self.api_key,
            'proxies': self.proxies,
            'bad_proxies': self.bad_proxies,
            'proxy_file': self.proxy_file,
            'settings': self.settings,
            'history': self.history,
            'is_active': self.is_active,
            'expiry_date': self.expiry_date
        }
        with open(self.data_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, user_id: int) -> 'UserData':
        path = f"user_data/user_{user_id}.json"
        user_data = cls(user_id)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                user_data.api_key = data.get('api_key')
                user_data.proxies = data.get('proxies', [])
                user_data.bad_proxies = data.get('bad_proxies', [])
                user_data.proxy_file = data.get('proxy_file')
                user_data.settings.update(data.get('settings', {}))
                user_data.history = data.get('history', [])
                user_data.is_active = data.get('is_active', False)
                user_data.expiry_date = data.get('expiry_date')
            except Exception as e:
                logger.error(f"Ошибка загрузки данных пользователя {user_id}: {e}")
        return user_data


# ===================== МЕНЕДЖЕР ПАРСЕРОВ =====================
class ParserManager:
    def __init__(self):
        self.parsers: List[MegafonCaptchaParser] = []
        self.threads: List[threading.Thread] = []
        self.shared_numbers: Set[str] = set()
        self.shared_duplicates: Dict[str, int] = {}
        self.lock = threading.Lock()
        self.is_running = False

    def start_parsers(self, rucaptcha_key: str, region: str, digits_count: int, num_threads: int,
                     proxy_configs: List[Optional[Dict]] = None,
                     min_delay: int = 10, max_delay: int = 30, session_delay: int = 60,
                     base_folder: str = "megafon_parser_results"):
        self.stop_parsers()
        self.parsers.clear()
        self.threads.clear()
        self.is_running = True
        self.shared_numbers.clear()
        self.shared_duplicates.clear()

        proxy_pool = proxy_configs if proxy_configs else []
        num_proxies = len(proxy_pool)

        for i in range(num_threads):
            proxy = None
            if num_proxies > 0:
                idx = i % num_proxies
                proxy = proxy_pool[idx]

            parser = MegafonCaptchaParser(
                rucaptcha_api_key=rucaptcha_key,
                proxy_config=proxy,
                region=region,
                thread_id=i+1,
                gui_callback=None,
                base_folder=base_folder,
                shared_numbers=self.shared_numbers,
                shared_duplicates=self.shared_duplicates,
                digits_count=digits_count
            )
            parser.delay_settings['min_delay'] = min_delay
            parser.delay_settings['max_delay'] = max_delay
            parser.delay_settings['session_delay'] = session_delay

            self.parsers.append(parser)

            thread = threading.Thread(target=parser.start, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop_parsers(self):
        self.is_running = False
        for parser in self.parsers:
            parser.stop()
        self.parsers.clear()
        self.threads.clear()

    def get_stats(self) -> Dict:
        total_numbers = len(self.shared_numbers)
        total_duplicates = sum(self.shared_duplicates.values())
        total_combinations = 0
        total_captchas = 0
        active_count = 0
        threads_info = []

        for parser in self.parsers:
            try:
                s = parser.get_stats()
                threads_info.append(s)
                total_combinations += s.get('combinations_tried', 0)
                total_captchas += s.get('captchas_solved', 0)
                if s.get('is_running'):
                    active_count += 1
            except:
                pass

        return {
            'total_numbers': total_numbers,
            'total_duplicates': total_duplicates,
            'total_combinations': total_combinations,
            'total_captchas': total_captchas,
            'active_parsers': active_count,
            'total_parsers': len(self.parsers),
            'threads': threads_info,
            'is_running': self.is_running
        }

    def get_numbers(self, limit: int = 100) -> List[str]:
        with self.lock:
            sorted_nums = sorted(self.shared_numbers)
            return sorted_nums[:limit]

    def get_all_numbers(self) -> List[str]:
        with self.lock:
            return sorted(self.shared_numbers)


# ===================== СОСТОЯНИЯ FSM =====================
class RunParserStates(StatesGroup):
    waiting_for_region = State()
    waiting_for_digits = State()
    waiting_for_threads = State()

class ProxyLoadStates(StatesGroup):
    waiting_for_choice = State()
    waiting_for_filepath = State()

class ApiKeyStates(StatesGroup):
    waiting_for_key = State()

class DelayStates(StatesGroup):
    waiting_for_min = State()
    waiting_for_max = State()

class ActivationStates(StatesGroup):
    waiting_for_key = State()

class AdminGenKeyStates(StatesGroup):
    waiting_for_days = State()
    waiting_for_target_id = State()

class AdminDeactivateKeyStates(StatesGroup):
    waiting_for_key = State()


# ===================== INLINE КЛАВИАТУРЫ =====================
def main_menu_kb(is_admin: bool = False, is_active: bool = False):
    if not is_active and not is_admin:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Активировать ключ", callback_data="activate_key")],
        ])

    buttons = [
        [InlineKeyboardButton(text="🚀 Запустить парсер", callback_data="run_parser")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="show_status"),
         InlineKeyboardButton(text="📞 Мои номера", callback_data="show_numbers")],
        [InlineKeyboardButton(text="🛰 Управление прокси", callback_data="proxy_menu")],
        [InlineKeyboardButton(text="🔑 Установить API ключ", callback_data="set_apikey")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"),
         InlineKeyboardButton(text="📜 История", callback_data="show_history")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def proxy_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Загрузить прокси", callback_data="proxy_load")],
        [InlineKeyboardButton(text="🧪 Проверить прокси", callback_data="proxy_test")],
        [InlineKeyboardButton(text="🗑 Удалить нерабочие", callback_data="proxy_remove")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
    ])

def proxy_choice_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Заменить", callback_data="proxy_replace"),
         InlineKeyboardButton(text="➕ Добавить", callback_data="proxy_append")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="proxy_menu")]
    ])

def settings_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱ Установить задержки", callback_data="set_delay")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
    ])

def back_to_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="main_menu")]
    ])

def stop_parser_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏹ Остановить парсер", callback_data="stop_parser")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

def download_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать номера", callback_data="download_numbers")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

def history_kb(history: list, page: int = 0):
    buttons = []
    start = page * 10
    end = start + 10
    for i, session in enumerate(history[start:end], start=start):
        start_time = datetime.fromisoformat(session['start_time']).strftime('%d.%m %H:%M')
        end_time = datetime.fromisoformat(session['end_time']).strftime('%H:%M') if session.get('end_time') else 'не завершён'
        count = session['numbers_count']
        buttons.append([InlineKeyboardButton(
            text=f"{start_time} - {end_time} | {count} шт.",
            callback_data=f"view_session_{i}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"history_page_{page-1}"))
    if end < len(history):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"history_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def session_view_kb(session_index: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать номера", callback_data=f"download_session_{session_index}")],
        [InlineKeyboardButton(text="◀️ К истории", callback_data="show_history"),
         InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Сгенерировать ключ", callback_data="admin_gen_key")],
        [InlineKeyboardButton(text="❌ Деактивировать ключ", callback_data="admin_deactivate_key")],
        [InlineKeyboardButton(text="📋 Список ключей", callback_data="admin_list_keys")],
        [InlineKeyboardButton(text="👥 Активные пользователи", callback_data="admin_list_users")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
    ])

def admin_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

def admin_skip_target_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить (универсальный ключ)", callback_data="admin_skip_target")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_panel")]
    ])

def admin_keys_list_kb(keys: list, page: int = 0):
    """Клавиатура со списком ключей и кнопками деактивации"""
    buttons = []
    start = page * 5
    end = start + 5
    for key_data in keys[start:end]:
        key = key_data['key']
        status = key_data['status']
        activated = '✅' if key_data['activated_by'] else '❌'
        status_emoji = '🟢' if status == 'active' else ('🔵' if status == 'used' else ('🔴' if status == 'deactivated' else '⚫'))
        btn_text = f"{status_emoji} {key[:8]}... ({status})"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"admin_view_key_{key}"),
            InlineKeyboardButton(text="❌ Деакт.", callback_data=f"admin_deactivate_key_{key}")
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin_keys_page_{page-1}"))
    if end < len(keys):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin_keys_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ===================== ОСНОВНОЙ БОТ =====================
class MegafonTelegramBot:
    def __init__(self, token: str):
        self.bot = Bot(token=token)
        self.dp = Dispatcher()

        self.user_data: Dict[int, UserData] = {}
        self.user_parsers: Dict[int, ParserManager] = {}
        self.user_menu_message: Dict[int, int] = {}

        self.activation_manager = ActivationManager()

        self._register_handlers()

    # ---------- ЗАГРУЗКА/СОХРАНЕНИЕ ДАННЫХ ----------
    def _get_user_data(self, user_id: int) -> UserData:
        if user_id not in self.user_data:
            ud = UserData.load(user_id)
            if not ud.is_active and self.activation_manager.is_subscription_active(user_id):
                expiry = self.activation_manager.get_user_subscription_expiry(user_id)
                if expiry:
                    ud.is_active = True
                    ud.expiry_date = expiry.isoformat()
                    ud.save()
            self.user_data[user_id] = ud
        return self.user_data[user_id]

    def _save_user_data(self, user_id: int):
        if user_id in self.user_data:
            self.user_data[user_id].save()

    # ---------- ПОЛУЧЕНИЕ МЕНЕДЖЕРОВ ----------
    def _get_parser_manager(self, user_id: int) -> ParserManager:
        if user_id not in self.user_parsers:
            self.user_parsers[user_id] = ParserManager()
        return self.user_parsers[user_id]

    def _get_proxy_manager(self, user_id: int) -> ProxyManager:
        pm = ProxyManager()
        user_data = self._get_user_data(user_id)
        pm.proxies = user_data.proxies.copy()
        pm.bad_proxies = user_data.bad_proxies.copy()
        pm.proxy_file = user_data.proxy_file
        return pm

    def _save_proxy_manager(self, user_id: int, pm: ProxyManager):
        user_data = self._get_user_data(user_id)
        user_data.proxies = pm.proxies.copy()
        user_data.bad_proxies = pm.bad_proxies.copy()
        user_data.proxy_file = pm.proxy_file
        self._save_user_data(user_id)

    # ---------- РАБОТА С СЕССИЯМИ ПАРСИНГА ----------
    async def _start_parser_session(self, user_id: int, region: str, digits: int, threads: int):
        user_data = self._get_user_data(user_id)
        if user_data.current_session:
            await self._finish_parser_session(user_id, stopped_manually=False)
        session = {
            'start_time': datetime.now().isoformat(),
            'end_time': None,
            'region': region,
            'digits': digits,
            'threads': threads,
            'numbers_count': 0,
            'numbers': []
        }
        user_data.current_session = session
        user_data.history.append(session)
        self._save_user_data(user_id)

    async def _finish_parser_session(self, user_id: int, stopped_manually: bool = True):
        user_data = self._get_user_data(user_id)
        parser_manager = self._get_parser_manager(user_id)
        if user_data.current_session and not user_data.current_session.get('end_time'):
            user_data.current_session['end_time'] = datetime.now().isoformat()
            numbers = parser_manager.get_all_numbers()
            user_data.current_session['numbers'] = numbers
            user_data.current_session['numbers_count'] = len(numbers)
            user_data.current_session['stopped_manually'] = stopped_manually
            user_data.current_session = None
            self._save_user_data(user_id)

    # ---------- ПРОВЕРКА ДОСТУПА ----------
    async def is_allowed(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return True
        user_data = self._get_user_data(user_id)
        if user_data.is_active and user_data.expiry_date:
            expiry = datetime.fromisoformat(user_data.expiry_date)
            if expiry > datetime.now():
                return True
            else:
                user_data.is_active = False
                user_data.expiry_date = None
                self._save_user_data(user_id)
                return False
        return False

    async def check_access(self, user_id: int, callback: CallbackQuery = None, message: Message = None):
        if not await self.is_allowed(user_id):
            if callback:
                await callback.answer("⛔ Доступ запрещён. Необходима активация.", show_alert=True)
            elif message:
                await message.answer("⛔ Доступ запрещён. Необходима активация.")
            return False
        return True

    # ---------- РАБОТА С ЕДИНЫМ МЕНЮ ----------
    async def update_menu(self, user_id: int, text: str, reply_markup: InlineKeyboardMarkup = None):
        try:
            if user_id in self.user_menu_message:
                await self.bot.edit_message_text(
                    text,
                    chat_id=user_id,
                    message_id=self.user_menu_message[user_id],
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            else:
                msg = await self.bot.send_message(
                    user_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
                self.user_menu_message[user_id] = msg.message_id
        except Exception as e:
            logger.error(f"Ошибка обновления меню для {user_id}: {e}")
            try:
                if user_id in self.user_menu_message:
                    await self.bot.delete_message(user_id, self.user_menu_message[user_id])
            except:
                pass
            msg = await self.bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
            self.user_menu_message[user_id] = msg.message_id

    async def delete_user_message(self, message: Message):
        try:
            await message.delete()
        except:
            pass

    # ---------- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ----------
    def _register_handlers(self):
        self.dp.message.register(self.cmd_start, Command("start"))
        self.dp.message.register(self.cmd_cancel, Command("cancel"))

        # FSM
        self.dp.message.register(self.process_region, RunParserStates.waiting_for_region)
        self.dp.message.register(self.process_digits, RunParserStates.waiting_for_digits)
        self.dp.message.register(self.process_threads, RunParserStates.waiting_for_threads)
        self.dp.message.register(self.process_apikey, ApiKeyStates.waiting_for_key)
        self.dp.message.register(self.process_delay_min, DelayStates.waiting_for_min)
        self.dp.message.register(self.process_delay_max, DelayStates.waiting_for_max)
        self.dp.message.register(self.handle_document, F.document, ProxyLoadStates.waiting_for_filepath)

        self.dp.message.register(self.process_activation_key, ActivationStates.waiting_for_key)
        self.dp.message.register(self.process_admin_gen_days, AdminGenKeyStates.waiting_for_days)
        self.dp.message.register(self.process_admin_gen_target_id, AdminGenKeyStates.waiting_for_target_id)
        self.dp.message.register(self.process_admin_deactivate_key, AdminDeactivateKeyStates.waiting_for_key)

        # Callback-запросы
        self.dp.callback_query.register(self.cb_main_menu, F.data == "main_menu")
        self.dp.callback_query.register(self.cb_run_parser, F.data == "run_parser")
        self.dp.callback_query.register(self.cb_show_status, F.data == "show_status")
        self.dp.callback_query.register(self.cb_show_numbers, F.data == "show_numbers")
        self.dp.callback_query.register(self.cb_proxy_menu, F.data == "proxy_menu")
        self.dp.callback_query.register(self.cb_proxy_load, F.data == "proxy_load")
        self.dp.callback_query.register(self.cb_proxy_replace, F.data == "proxy_replace")
        self.dp.callback_query.register(self.cb_proxy_append, F.data == "proxy_append")
        self.dp.callback_query.register(self.cb_proxy_test, F.data == "proxy_test")
        self.dp.callback_query.register(self.cb_proxy_remove, F.data == "proxy_remove")
        self.dp.callback_query.register(self.cb_set_apikey, F.data == "set_apikey")
        self.dp.callback_query.register(self.cb_settings_menu, F.data == "settings_menu")
        self.dp.callback_query.register(self.cb_set_delay, F.data == "set_delay")
        self.dp.callback_query.register(self.cb_stop_parser, F.data == "stop_parser")
        self.dp.callback_query.register(self.cb_download_numbers, F.data == "download_numbers")

        self.dp.callback_query.register(self.cb_show_history, F.data == "show_history")
        self.dp.callback_query.register(self.cb_history_page, F.data.startswith("history_page_"))
        self.dp.callback_query.register(self.cb_view_session, F.data.startswith("view_session_"))
        self.dp.callback_query.register(self.cb_download_session, F.data.startswith("download_session_"))

        self.dp.callback_query.register(self.cb_activate_key, F.data == "activate_key")
        self.dp.callback_query.register(self.cb_admin_panel, F.data == "admin_panel")
        self.dp.callback_query.register(self.cb_admin_gen_key, F.data == "admin_gen_key")
        self.dp.callback_query.register(self.cb_admin_deactivate_key, F.data == "admin_deactivate_key")
        self.dp.callback_query.register(self.cb_admin_list_keys, F.data == "admin_list_keys")
        self.dp.callback_query.register(self.cb_admin_list_users, F.data == "admin_list_users")
        self.dp.callback_query.register(self.cb_admin_skip_target, F.data == "admin_skip_target")
        self.dp.callback_query.register(self.cb_admin_deactivate_key_callback, F.data.startswith("admin_deactivate_key_"))
        self.dp.callback_query.register(self.cb_admin_keys_page, F.data.startswith("admin_keys_page_"))
        self.dp.callback_query.register(self.cb_admin_view_key, F.data.startswith("admin_view_key_"))

    # ---------- СТАРТ (с отображением Telegram ID) ----------
    async def cmd_start(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        await self.delete_user_message(message)
        await state.clear()

        is_admin = user_id in ADMIN_IDS
        is_active = await self.is_allowed(user_id)

        id_text = f"🆔 <b>Ваш Telegram ID:</b> <code>{user_id}</code>\n\n"

        if not is_active and not is_admin:
            text = (
                f"👋 <b>Megafon Parser Bot</b>\n\n"
                f"{id_text}"
                f"Для использования бота необходимо активировать ключ.\n"
                f"Отправьте этот ID администратору, чтобы получить ключ активации."
            )
        else:
            text = f"👋 <b>Megafon Parser Bot</b>\n\n{id_text}Выберите действие:"
            if is_active and not is_admin:
                ud = self._get_user_data(user_id)
                expiry = datetime.fromisoformat(ud.expiry_date).strftime("%d.%m.%Y %H:%M")
                text = f"👋 <b>Megafon Parser Bot</b>\n\n{id_text}✅ Подписка активна до: {expiry}\n\nВыберите действие:"

        await self.update_menu(
            user_id,
            text,
            main_menu_kb(is_admin=is_admin, is_active=is_active or is_admin)
        )

    async def cmd_cancel(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        await self.delete_user_message(message)
        current = await state.get_state()
        if current:
            await state.clear()
            await self.update_menu(
                user_id,
                "✅ Действие отменено.\n\n👋 <b>Главное меню</b>",
                main_menu_kb(is_admin=user_id in ADMIN_IDS,
                            is_active=await self.is_allowed(user_id))
            )
        else:
            await self.cmd_start(message, state)

    # ---------- ВОЗВРАТ В ГЛАВНОЕ МЕНЮ ----------
    async def cb_main_menu(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        await state.clear()
        is_admin = user_id in ADMIN_IDS
        is_active = await self.is_allowed(user_id)

        id_text = f"🆔 <b>Ваш Telegram ID:</b> <code>{user_id}</code>\n\n"

        if not is_active and not is_admin:
            text = (
                f"👋 <b>Megafon Parser Bot</b>\n\n"
                f"{id_text}"
                f"Для использования бота необходимо активировать ключ.\n"
                f"Отправьте этот ID администратору, чтобы получить ключ активации."
            )
        else:
            text = f"👋 <b>Главное меню</b>\n\n{id_text}Выберите действие:"

        await self.update_menu(
            user_id,
            text,
            main_menu_kb(is_admin=is_admin, is_active=is_active or is_admin)
        )

    # ---------- АКТИВАЦИЯ КЛЮЧА ----------
    async def cb_activate_key(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        if await self.is_allowed(user_id) or user_id in ADMIN_IDS:
            await callback.answer("✅ Ваш доступ уже активирован.", show_alert=True)
            return

        await self.update_menu(
            user_id,
            "🔑 Введите ключ активации, который вы получили от администратора:",
            back_to_main_kb()
        )
        await state.set_state(ActivationStates.waiting_for_key)

    async def process_activation_key(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        await self.delete_user_message(message)

        key = message.text.strip().upper()
        success, result = self.activation_manager.activate_key(key, user_id)

        if success:
            user_data = self._get_user_data(user_id)
            user_data.is_active = True
            user_data.expiry_date = result
            user_data.save()

            expiry_date = datetime.fromisoformat(result).strftime("%d.%m.%Y %H:%M")
            await self.update_menu(
                user_id,
                f"✅ Ключ успешно активирован!\n\nВаша подписка действует до: {expiry_date}",
                main_menu_kb(is_admin=False, is_active=True)
            )
        else:
            await self.update_menu(
                user_id,
                f"{result}\n\nПопробуйте ещё раз или обратитесь к администратору.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔑 Ввести другой ключ", callback_data="activate_key")],
                    [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
                ])
            )
        await state.clear()

    # ---------- АДМИН-ПАНЕЛЬ ----------
    async def cb_admin_panel(self, callback: CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        await self.update_menu(
            user_id,
            "<b>🛠 Админ-панель</b>\n\nВыберите действие:",
            admin_panel_kb()
        )

    # ---------- ГЕНЕРАЦИЯ КЛЮЧА ----------
    async def cb_admin_gen_key(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        await self.update_menu(
            user_id,
            "🔑 Введите количество <b>дней</b> действия подписки для этого ключа:",
            admin_skip_target_kb()
        )
        await state.set_state(AdminGenKeyStates.waiting_for_days)

    async def process_admin_gen_days(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        if user_id not in ADMIN_IDS:
            await self.delete_user_message(message)
            return

        await self.delete_user_message(message)

        try:
            days = int(message.text.strip())
            if days <= 0:
                raise ValueError
        except:
            await self.update_menu(
                user_id,
                "❌ Введите положительное целое число.\n\nВведите количество дней:",
                admin_skip_target_kb()
            )
            return

        await state.update_data(days=days)
        await self.update_menu(
            user_id,
            "👤 Введите <b>Telegram ID пользователя</b>, для которого предназначен ключ.\n"
            "Или нажмите кнопку ниже, чтобы создать универсальный ключ.",
            admin_skip_target_kb()
        )
        await state.set_state(AdminGenKeyStates.waiting_for_target_id)

    async def cb_admin_skip_target(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            return

        data = await state.get_data()
        days = data.get('days', 30)
        key = self.activation_manager.create_key(user_id, days, target_user_id=None)

        expiry_preview = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")
        await self.update_menu(
            user_id,
            f"✅ Универсальный ключ успешно сгенерирован:\n\n"
            f"<code>{key}</code>\n\n"
            f"Срок действия ключа (до активации): {expiry_preview}\n"
            f"После активации пользователь получит подписку на {days} дн.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        await state.clear()

    async def process_admin_gen_target_id(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        if user_id not in ADMIN_IDS:
            await self.delete_user_message(message)
            return

        await self.delete_user_message(message)

        target_id_str = message.text.strip()
        try:
            target_id = int(target_id_str)
        except ValueError:
            await self.update_menu(
                user_id,
                "❌ Некорректный ID. Введите число или нажмите кнопку пропуска.",
                admin_skip_target_kb()
            )
            return

        data = await state.get_data()
        days = data.get('days', 30)
        key = self.activation_manager.create_key(user_id, days, target_user_id=target_id)

        expiry_preview = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y %H:%M")
        await self.update_menu(
            user_id,
            f"✅ Ключ для пользователя <code>{target_id}</code> успешно сгенерирован:\n\n"
            f"<code>{key}</code>\n\n"
            f"Срок действия ключа (до активации): {expiry_preview}\n"
            f"После активации пользователь получит подписку на {days} дн.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        await state.clear()

    # ---------- ДЕАКТИВАЦИЯ КЛЮЧА ----------
    async def cb_admin_deactivate_key(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        await self.update_menu(
            user_id,
            "❌ Введите ключ, который необходимо деактивировать:",
            admin_back_kb()
        )
        await state.set_state(AdminDeactivateKeyStates.waiting_for_key)

    async def process_admin_deactivate_key(self, message: Message, state: FSMContext):
        user_id = message.from_user.id
        if user_id not in ADMIN_IDS:
            await self.delete_user_message(message)
            return

        await self.delete_user_message(message)

        key = message.text.strip().upper()
        success, affected_user, msg = self.activation_manager.deactivate_key(key)

        if success and affected_user:
            # Обновляем данные пользователя, если он есть в кэше
            if affected_user in self.user_data:
                ud = self.user_data[affected_user]
                ud.is_active = False
                ud.expiry_date = None
                ud.save()
            # Также можно попытаться загрузить данные, если их нет в кэше
            else:
                ud = UserData.load(affected_user)
                ud.is_active = False
                ud.expiry_date = None
                ud.save()

        await self.update_menu(
            user_id,
            msg,
            admin_back_kb()
        )
        await state.clear()

    async def cb_admin_deactivate_key_callback(self, callback: CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        key = callback.data.split('_')[-1]
        success, affected_user, msg = self.activation_manager.deactivate_key(key)

        if success and affected_user:
            if affected_user in self.user_data:
                ud = self.user_data[affected_user]
                ud.is_active = False
                ud.expiry_date = None
                ud.save()
            else:
                ud = UserData.load(affected_user)
                ud.is_active = False
                ud.expiry_date = None
                ud.save()

        await callback.message.answer(msg)  # отдельное сообщение
        # Обновим список ключей
        await self.cb_admin_list_keys(callback)

    # ---------- ПРОСМОТР КЛЮЧЕЙ ----------
    async def cb_admin_list_keys(self, callback: CallbackQuery, page: int = 0):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        keys_dict = self.activation_manager.get_all_keys()
        keys_list = list(keys_dict.values())
        # Сортируем по дате создания (новые сверху)
        keys_list.sort(key=lambda x: x['created_at'], reverse=True)

        if not keys_list:
            await self.update_menu(
                user_id,
                "📭 Нет сгенерированных ключей.",
                admin_back_kb()
            )
            return

        await self.update_menu(
            user_id,
            f"<b>📋 Список ключей (страница {page+1}):</b>\n\n",
            admin_keys_list_kb(keys_list, page)
        )

    async def cb_admin_keys_page(self, callback: CallbackQuery):
        page = int(callback.data.split('_')[-1])
        await self.cb_admin_list_keys(callback, page)

    async def cb_admin_view_key(self, callback: CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            return

        key = callback.data.split('_')[-1]
        key_data = self.activation_manager.activations.get(key)
        if not key_data:
            await callback.answer("❌ Ключ не найден.", show_alert=True)
            return

        created = datetime.fromisoformat(key_data['created_at']).strftime("%d.%m.%Y %H:%M")
        expiry = datetime.fromisoformat(key_data['expiry']).strftime("%d.%m.%Y %H:%M")
        status = key_data['status']
        target = key_data.get('target_user_id', '—')
        activated_by = key_data.get('activated_by', '—')
        activated_at = key_data.get('activated_at')
        if activated_at:
            activated_at = datetime.fromisoformat(activated_at).strftime("%d.%m.%Y %H:%M")
        sub_expiry = key_data.get('subscription_expiry')
        if sub_expiry:
            sub_expiry = datetime.fromisoformat(sub_expiry).strftime("%d.%m.%Y %H:%M")

        text = (
            f"<b>🔑 Детали ключа</b>\n\n"
            f"Ключ: <code>{key}</code>\n"
            f"Статус: {status}\n"
            f"Создан: {created}\n"
            f"Истекает: {expiry}\n"
            f"Дней подписки: {key_data['days']}\n"
            f"Целевой ID: {target}\n"
            f"Активирован пользователем: {activated_by}\n"
            f"Дата активации: {activated_at or '—'}\n"
            f"Подписка до: {sub_expiry or '—'}\n"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Деактивировать", callback_data=f"admin_deactivate_key_{key}")],
            [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="admin_list_keys")],
            [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")]
        ])
        await self.update_menu(user_id, text, kb)

    async def cb_admin_list_users(self, callback: CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        if user_id not in ADMIN_IDS:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
            return

        users = {}
        for key, data in self.activation_manager.get_all_keys().items():
            if data.get('activated_by') and data.get('status') != 'deactivated':
                uid = data['activated_by']
                if uid not in users:
                    expiry = data.get('subscription_expiry')
                    users[uid] = {
                        'expiry': expiry,
                        'activated_at': data.get('activated_at'),
                        'key': key
                    }

        if not users:
            await self.update_menu(
                user_id,
                "👥 Нет активных пользователей.",
                admin_back_kb()
            )
            return

        text = "<b>👥 Активные пользователи:</b>\n\n"
        for uid, info in users.items():
            activated = datetime.fromisoformat(info['activated_at']).strftime("%d.%m.%Y %H:%M") if info['activated_at'] else '?'
            expiry = datetime.fromisoformat(info['expiry']).strftime("%d.%m.%Y %H:%M") if info['expiry'] else '?'
            text += f"🆔 <code>{uid}</code>\n"
            text += f"   Активирован: {activated}\n"
            text += f"   Подписка до: {expiry}\n"
            text += f"   Ключ: <code>{info['key'][:8]}...</code>\n\n"

        await self.update_menu(user_id, text, admin_back_kb())

    # ---------- ЗАПУСК ПАРСЕРА ----------
    async def cb_run_parser(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        user_id = callback.from_user.id
        if not await self.check_access(user_id, callback=callback):
            return

        user_data = self._get_user_data(user_id)

        if not user_data.api_key:
            await self.update_menu(
                user_id,
                "⚠️ <b>API ключ RuCaptcha не установлен.</b>\n\n"
                "Для запуска парсера необходимо установить личный ключ.\n"
                "Воспользуйтесь кнопкой ниже:",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔑 Установить ключ", callback_data="set_apikey")],
                    [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
                ])
            )
            return

        await self.update_menu(
            user_id,
            "🚀 <b>Запуск парсера</b>\n\nВведите <b>регион</b> (например: moscow, spb, nnov):",
            back_to_main_kb()
        )
        await state.set_state(RunParserStates.waiting_for_region)

    # ---------- ОСТАНОВКА ПАРСЕРА ----------
    async def cb_stop_parser(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        parser_manager = self._get_parser_manager(user_id)

        if not parser_manager.is_running:
            await callback.answer("❌ Парсер уже остановлен.", show_alert=True)
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, parser_manager.stop_parsers)

        await self._finish_parser_session(user_id, stopped_manually=True)

        await self.update_menu(
            user_id,
            "⏹ Парсер остановлен. Результаты сохранены в историю.",
            back_to_main_kb()
        )

    # ---------- СТАТИСТИКА ----------
    async def cb_show_status(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        parser_manager = self._get_parser_manager(user_id)
        stats = parser_manager.get_stats()

        if not stats['total_parsers']:
            await self.update_menu(
                user_id,
                "ℹ️ У вас нет активных парсеров.",
                back_to_main_kb()
            )
            return

        lines = []
        lines.append(f"<b>📊 ВАША СТАТИСТИКА</b>\n")
        lines.append(f"📞 Найдено номеров: {stats['total_numbers']}")
        lines.append(f"🔄 Дубликатов: {stats['total_duplicates']}")
        lines.append(f"🔢 Всего комбинаций: {stats['total_combinations']}")
        lines.append(f"🧩 Капч решено: {stats['total_captchas']}")
        lines.append(f"🟢 Активных потоков: {stats['active_parsers']}/{stats['total_parsers']}\n")

        for thr in stats['threads'][:5]:
            lines.append(
                f"Поток {thr.get('thread_id')} ({thr.get('region')}):\n"
                f"  Статус: {thr.get('status')}\n"
                f"  Номеров: {thr.get('numbers_found')}\n"
                f"  Комб: {thr.get('combinations_tried')}\n"
                f"  Капч: {thr.get('captchas_solved')}\n"
                f"  Текущая: {thr.get('current_combination') or '—'}\n"
            )

        buttons = []
        if stats['is_running']:
            buttons.append([InlineKeyboardButton(text="⏹ Остановить", callback_data="stop_parser")])
        buttons.append([InlineKeyboardButton(text="📥 Скачать номера", callback_data="download_numbers")])
        buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")])

        await self.update_menu(
            user_id,
            "\n".join(lines),
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )

    # ---------- ПОКАЗАТЬ НОМЕРА ----------
    async def cb_show_numbers(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        parser_manager = self._get_parser_manager(user_id)
        numbers = parser_manager.get_numbers(limit=50)

        if not numbers:
            await self.update_menu(
                user_id,
                "📭 Вы ещё не нашли номера в текущей сессии.",
                back_to_main_kb()
            )
            return

        text = "<b>📞 Ваши найденные номера (текущая сессия, первые 50):</b>\n\n"
        text += "\n".join([f"+7{num}" for num in numbers])
        await self.update_menu(user_id, text, download_kb())

    # ---------- СКАЧИВАНИЕ НОМЕРОВ (текущей сессии) ----------
    async def cb_download_numbers(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        parser_manager = self._get_parser_manager(user_id)
        numbers = parser_manager.get_all_numbers()

        if not numbers:
            await callback.answer("📭 Нет найденных номеров в текущей сессии.", show_alert=True)
            return

        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as f:
            for num in numbers:
                f.write(f"+7{num}\n")
            temp_path = f.name

        await callback.message.answer_document(
            document=FSInputFile(temp_path, filename=f"megafon_numbers_current_{user_id}.txt"),
            caption=f"📞 Текущая сессия: всего номеров {len(numbers)}"
        )
        os.remove(temp_path)

    # ---------- ПРОКСИ ----------
    async def cb_proxy_menu(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        pm = self._get_proxy_manager(user_id)
        await self.update_menu(
            user_id,
            f"🛰 <b>Управление прокси</b>\n\n"
            f"📡 Загружено прокси: {len(pm.proxies)}\n"
            f"⚠️ Нерабочих: {len(pm.bad_proxies)}\n\n"
            f"Выберите действие:",
            proxy_menu_kb()
        )

    async def cb_proxy_load(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        pm = self._get_proxy_manager(user_id)
        if pm.proxies:
            await self.update_menu(
                user_id,
                f"📎 У вас уже загружено {len(pm.proxies)} прокси.\n\n"
                f"Выберите действие:",
                proxy_choice_kb()
            )
            await state.set_state(ProxyLoadStates.waiting_for_choice)
        else:
            await self.update_menu(
                user_id,
                "📎 Отправьте <b>файл с прокси</b> (.txt)",
                back_to_main_kb()
            )
            await state.update_data(proxy_mode="replace")
            await state.set_state(ProxyLoadStates.waiting_for_filepath)

    async def cb_proxy_replace(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        await state.update_data(proxy_mode="replace")
        await self.update_menu(
            callback.from_user.id,
            "📎 Отправьте <b>файл с прокси</b> (.txt)\n\nСтарые прокси будут заменены.",
            back_to_main_kb()
        )
        await state.set_state(ProxyLoadStates.waiting_for_filepath)

    async def cb_proxy_append(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        await state.update_data(proxy_mode="append")
        await self.update_menu(
            callback.from_user.id,
            "📎 Отправьте <b>файл с прокси</b> (.txt)\n\nНовые прокси будут добавлены к существующим.",
            back_to_main_kb()
        )
        await state.set_state(ProxyLoadStates.waiting_for_filepath)

    async def handle_document(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return

        user_id = message.from_user.id
        await self.delete_user_message(message)

        current_state = await state.get_state()
        if current_state != ProxyLoadStates.waiting_for_filepath.state:
            return

        data = await state.get_data()
        mode = data.get('proxy_mode', 'replace')

        document = message.document
        if not document.file_name.endswith('.txt'):
            await self.update_menu(
                user_id,
                "❌ Пожалуйста, отправьте файл с расширением .txt",
                proxy_menu_kb()
            )
            await state.clear()
            return

        pm = self._get_proxy_manager(user_id)

        file = await self.bot.get_file(document.file_id)
        file_path = f"temp_proxy_{user_id}.txt"
        await self.bot.download_file(file.file_path, file_path)

        count = pm.load_proxies(file_path, mode=mode)
        os.remove(file_path)

        self._save_proxy_manager(user_id, pm)

        await self.update_menu(
            user_id,
            f"✅ Загружено {count} прокси (режим: {'замена' if mode=='replace' else 'добавление'}).\n\n"
            f"📡 Всего прокси: {len(pm.proxies)}",
            proxy_menu_kb()
        )
        await state.clear()

    async def cb_proxy_test(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        pm = self._get_proxy_manager(user_id)

        if not pm.proxies:
            await callback.answer("⚠️ Сначала загрузите прокси.", show_alert=True)
            return

        await self.update_menu(user_id, "🔄 Начинаю тестирование прокси...", None)
        total = len(pm.proxies)

        async def progress(cur, tot, proxy, ok):
            if cur % 5 == 0 or cur == tot:
                await self.update_menu(user_id, f"🔄 Проверено {cur}/{tot}...", None)

        working, bad = await pm.test_proxies(progress)
        self._save_proxy_manager(user_id, pm)

        await self.update_menu(
            user_id,
            f"✅ Тестирование завершено.\n"
            f"📊 Рабочих: {len(working)}\n"
            f"❌ Нерабочих: {len(bad)}",
            proxy_menu_kb()
        )

    async def cb_proxy_remove(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        pm = self._get_proxy_manager(user_id)

        if not pm.bad_proxies:
            await callback.answer("ℹ️ Нет нерабочих прокси для удаления.", show_alert=True)
            return

        removed, remaining = pm.remove_bad_proxies()
        self._save_proxy_manager(user_id, pm)
        await self.update_menu(
            user_id,
            f"✅ Удалено {removed} нерабочих прокси.\n📊 Осталось {remaining} рабочих.",
            proxy_menu_kb()
        )

    # ---------- API КЛЮЧ ----------
    async def cb_set_apikey(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        await self.update_menu(
            user_id,
            "🔑 Введите ваш API-ключ от <b>rucaptcha.com</b>.\n\n"
            "Ключ будет сохранён только для вашего аккаунта.",
            back_to_main_kb()
        )
        await state.set_state(ApiKeyStates.waiting_for_key)

    async def process_apikey(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        key = message.text.strip()
        await self.delete_user_message(message)

        if not re.match(r'^[a-f0-9]{32,}$', key, re.IGNORECASE):
            await self.update_menu(
                user_id,
                "❌ Некорректный ключ. Ожидается 32+ символов (hex).\n\n"
                "Введите API-ключ:",
                back_to_main_kb()
            )
            return

        user_data = self._get_user_data(user_id)
        user_data.api_key = key
        self._save_user_data(user_id)

        await state.clear()
        await self.update_menu(
            user_id,
            "✅ Ваш API-ключ успешно сохранён.",
            main_menu_kb(is_admin=user_id in ADMIN_IDS,
                        is_active=await self.is_allowed(user_id))
        )

    # ---------- НАСТРОЙКИ ----------
    async def cb_settings_menu(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        user_data = self._get_user_data(user_id)
        pm = self._get_proxy_manager(user_id)

        api_key = user_data.api_key or ''
        if api_key:
            masked_key = api_key[:6] + '*' * (len(api_key) - 10) + api_key[-4:] if len(api_key) > 10 else '***'
        else:
            masked_key = '❌ не установлен'

        text = (
            "<b>⚙️ ВАШИ ТЕКУЩИЕ НАСТРОЙКИ</b>\n\n"
            f"🕒 Мин. задержка: {user_data.settings['min_delay']} сек\n"
            f"🕒 Макс. задержка: {user_data.settings['max_delay']} сек\n"
            f"🕒 Задержка между сессиями: {user_data.settings['session_delay']} сек\n"
            f"🔑 API ключ: {masked_key}\n"
            f"📡 Прокси загружено: {len(pm.proxies)}\n"
            f"⚠️ Нерабочих прокси: {len(pm.bad_proxies)}"
        )
        await self.update_menu(user_id, text, settings_menu_kb())

    async def cb_set_delay(self, callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        await self.update_menu(
            user_id,
            "⏱ Введите <b>минимальную задержку</b> между запросами (в секундах):",
            back_to_main_kb()
        )
        await state.set_state(DelayStates.waiting_for_min)

    async def process_delay_min(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        await self.delete_user_message(message)

        try:
            min_d = int(message.text.strip())
            if min_d < 1:
                raise ValueError
        except:
            await self.update_menu(
                user_id,
                "❌ Введите целое число >= 1.\n\nВведите минимальную задержку:",
                back_to_main_kb()
            )
            return

        await state.update_data(min_delay=min_d)
        await self.update_menu(
            user_id,
            "⏱ Введите <b>максимальную задержку</b> между запросами (в секундах):",
            back_to_main_kb()
        )
        await state.set_state(DelayStates.waiting_for_max)

    async def process_delay_max(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        await self.delete_user_message(message)

        try:
            max_d = int(message.text.strip())
            if max_d < 1:
                raise ValueError
        except:
            await self.update_menu(
                user_id,
                "❌ Введите целое число >= 1.\n\nВведите максимальную задержку:",
                back_to_main_kb()
            )
            return

        data = await state.get_data()
        min_d = data.get('min_delay', 10)
        if max_d < min_d:
            await self.update_menu(
                user_id,
                "❌ Максимальная задержка не может быть меньше минимальной.\n\n"
                "Введите максимальную задержку:",
                back_to_main_kb()
            )
            return

        user_data = self._get_user_data(user_id)
        user_data.settings['min_delay'] = min_d
        user_data.settings['max_delay'] = max_d
        self._save_user_data(user_id)

        await state.clear()
        await self.update_menu(
            user_id,
            f"✅ Задержки установлены: мин {min_d} сек, макс {max_d} сек",
            main_menu_kb(is_admin=user_id in ADMIN_IDS,
                        is_active=await self.is_allowed(user_id))
        )

    # ---------- ИСТОРИЯ ПАРСИНГА ----------
    async def cb_show_history(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        user_data = self._get_user_data(user_id)

        if not user_data.history:
            await self.update_menu(
                user_id,
                "📜 У вас пока нет завершённых сессий парсинга.",
                back_to_main_kb()
            )
            return

        await self.update_menu(
            user_id,
            "<b>📜 История парсинга</b>\n\nВыберите сессию:",
            history_kb(user_data.history, 0)
        )

    async def cb_history_page(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        user_data = self._get_user_data(user_id)
        page = int(callback.data.split('_')[2])
        await self.update_menu(
            user_id,
            "<b>📜 История парсинга</b>\n\nВыберите сессию:",
            history_kb(user_data.history, page)
        )

    async def cb_view_session(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        user_data = self._get_user_data(user_id)
        session_index = int(callback.data.split('_')[2])
        session = user_data.history[session_index]

        start = datetime.fromisoformat(session['start_time']).strftime('%d.%m.%Y %H:%M:%S')
        end = datetime.fromisoformat(session['end_time']).strftime('%d.%m.%Y %H:%M:%S') if session.get('end_time') else 'не завершён'
        duration = ''
        if session.get('end_time'):
            delta = datetime.fromisoformat(session['end_time']) - datetime.fromisoformat(session['start_time'])
            hours = delta.seconds // 3600
            minutes = (delta.seconds % 3600) // 60
            duration = f"{hours}ч {minutes}м"

        text = (
            f"<b>📌 Сессия от {start}</b>\n\n"
            f"• Начало: {start}\n"
            f"• Окончание: {end}\n"
            f"• Длительность: {duration}\n"
            f"• Регион: {session['region']}\n"
            f"• Цифр: {session['digits']}\n"
            f"• Потоков: {session['threads']}\n"
            f"• Найдено номеров: {session['numbers_count']}\n"
        )
        if session.get('stopped_manually'):
            text += "• Остановлен вручную\n"
        else:
            text += "• Остановлен автоматически (перезапуск)\n"

        if session['numbers']:
            preview = "\n".join([f"+7{num}" for num in session['numbers'][:20]])
            if len(session['numbers']) > 20:
                preview += f"\n... и ещё {len(session['numbers'])-20}"
            text += f"\n<b>Номера (первые 20):</b>\n{preview}"

        await self.update_menu(
            user_id,
            text,
            session_view_kb(session_index)
        )

    async def cb_download_session(self, callback: CallbackQuery):
        await callback.answer()
        if not await self.check_access(callback.from_user.id, callback=callback):
            return
        user_id = callback.from_user.id
        user_data = self._get_user_data(user_id)
        session_index = int(callback.data.split('_')[2])
        session = user_data.history[session_index]

        if not session['numbers']:
            await callback.answer("📭 В этой сессии нет номеров.", show_alert=True)
            return

        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as f:
            for num in session['numbers']:
                f.write(f"+7{num}\n")
            temp_path = f.name

        start_date = datetime.fromisoformat(session['start_time']).strftime('%Y%m%d_%H%M')
        await callback.message.answer_document(
            document=FSInputFile(temp_path, filename=f"megafon_session_{start_date}_{user_id}.txt"),
            caption=f"📞 Сессия от {start_date}: всего номеров {session['numbers_count']}"
        )
        os.remove(temp_path)

    # ---------- ПАРСЕР ----------
    async def process_region(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        region = message.text.strip().lower()
        await self.delete_user_message(message)

        if not re.match(r'^[a-z]+$', region):
            await self.update_menu(
                user_id,
                "❌ Некорректный регион. Используйте латиницу, например: moscow\n\nВведите <b>регион</b>:",
                back_to_main_kb()
            )
            return

        await state.update_data(region=region)
        await self.update_menu(
            user_id,
            "Введите количество <b>цифр для поиска</b> (1-4):",
            back_to_main_kb()
        )
        await state.set_state(RunParserStates.waiting_for_digits)

    async def process_digits(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        await self.delete_user_message(message)

        try:
            digits = int(message.text.strip())
            if not 1 <= digits <= 4:
                raise ValueError
        except:
            await self.update_menu(
                user_id,
                "❌ Введите число от 1 до 4.\n\nВведите количество цифр:",
                back_to_main_kb()
            )
            return

        await state.update_data(digits=digits)
        await self.update_menu(
            user_id,
            "Введите количество <b>потоков</b> (1-10):",
            back_to_main_kb()
        )
        await state.set_state(RunParserStates.waiting_for_threads)

    async def process_threads(self, message: Message, state: FSMContext):
        if not await self.check_access(message.from_user.id, message=message):
            return
        user_id = message.from_user.id
        await self.delete_user_message(message)

        try:
            threads = int(message.text.strip())
            if not 1 <= threads <= 10:
                raise ValueError
        except:
            await self.update_menu(
                user_id,
                "❌ Введите число от 1 до 10.\n\nВведите количество потоков:",
                back_to_main_kb()
            )
            return

        data = await state.get_data()
        region = data.get('region', 'moscow')
        digits = data.get('digits', 4)

        await state.clear()

        parser_manager = self._get_parser_manager(user_id)
        user_data = self._get_user_data(user_id)
        pm = self._get_proxy_manager(user_id)

        proxy_configs = []
        if pm.proxies:
            for p in pm.proxies:
                cfg = pm.parse_proxy_string(p)
                if cfg:
                    proxy_configs.append(cfg)

        base_folder = f"megafon_parser_results/user_{user_id}"

        await self._start_parser_session(user_id, region, digits, threads)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: parser_manager.start_parsers(
                rucaptcha_key=user_data.api_key,
                region=region,
                digits_count=digits,
                num_threads=threads,
                proxy_configs=proxy_configs if proxy_configs else None,
                min_delay=user_data.settings['min_delay'],
                max_delay=user_data.settings['max_delay'],
                session_delay=user_data.settings['session_delay'],
                base_folder=base_folder
            )
        )

        await self.update_menu(
            user_id,
            f"✅ Парсер запущен!\n\n"
            f"• Регион: {region}\n"
            f"• Цифр: {digits}\n"
            f"• Потоков: {threads}",
            stop_parser_kb()
        )

    # ---------- ЗАПУСК ----------
    async def start(self):
        await self.dp.start_polling(self.bot)


async def main():
    bot_app = MegafonTelegramBot(BOT_TOKEN)
    await bot_app.start()

if __name__ == "__main__":
    asyncio.run(main())