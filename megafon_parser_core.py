import asyncio
import random
import base64
import logging
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import Dict, Any, Optional, List
from playwright.async_api import async_playwright, Page, Response
import aiohttp
import json
import re
from urllib.parse import quote, urlparse
import time
import concurrent.futures
import os
from datetime import datetime, timedelta
import subprocess
import sys

os.chdir(os.path.expanduser("~"))

class MegafonCaptchaParser:
    def __init__(self, rucaptcha_api_key: str = None, proxy_config: Optional[Dict] = None, region: str = "moscow", 
                 thread_id: int = 1, gui_callback=None, base_folder: str = "megafon_parser_results", 
                 shared_numbers: set = None, shared_duplicates: dict = None, digits_count: int = 4):
        self.region = region
        self.thread_id = thread_id
        self.gui_callback = gui_callback
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
        """Логирование с callback в GUI"""
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
            
            # Если это первый запрос
            if not self.delay_settings['last_request_time']:
                self.delay_settings['last_request_time'] = now
                return
            
            # Считаем время с последнего запроса
            time_since_last = (now - self.delay_settings['last_request_time']).total_seconds()
            
            # Если с последнего запроса прошло меньше минимальной задержки, ждем
            if time_since_last < self.delay_settings['min_delay']:
                wait_time = self.delay_settings['min_delay'] - time_since_last
                wait_time *= self.stats['delay_multiplier']
                self.log_message(f"⏳ Задержка для предотвращения капчи: {wait_time:.1f} сек")
                await asyncio.sleep(wait_time)
            
            # Увеличиваем множитель задержки если много капч
            if self.captcha_detected:
                self.stats['delay_multiplier'] = min(self.stats['delay_multiplier'] * 1.5, 5.0)
                self.log_message(f"📈 Увеличиваем задержку до {self.stats['delay_multiplier']:.1f}x")
            elif self.stats['delay_multiplier'] > 1.0:
                # Постепенно уменьшаем множитель если капч нет
                self.stats['delay_multiplier'] = max(self.stats['delay_multiplier'] * 0.9, 1.0)
            
            self.delay_settings['request_count'] += 1
            self.delay_settings['last_request_time'] = datetime.now()
            
            # Если сделано много запросов, делаем большую паузу
            if self.delay_settings['request_count'] % 10 == 0:
                long_delay = random.uniform(30, 60)
                self.log_message(f"⏳ Большая пауза после 10 запросов: {long_delay:.1f} сек")
                await asyncio.sleep(long_delay)
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в задержке: {e}", "WARNING")

    async def solve_captcha(self, captcha_image_base64: str) -> Optional[str]:
        """Решает капчу через RuCaptcha - улучшенная версия"""
        if not self.rucaptcha_api_key:
            self.log_message("❌ API ключ RuCaptcha не указан", "ERROR")
            return None
        
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                # Очищаем и проверяем base64
                if not captcha_image_base64 or len(captcha_image_base64) < 100:
                    self.log_message("❌ Получена пустая или слишком короткая base64 строка", "ERROR")
                    return None
                
                # Удаляем префикс data:image если есть
                if "base64," in captcha_image_base64:
                    captcha_image_base64 = captcha_image_base64.split("base64,")[1]
                
                # Очищаем base64
                captcha_image_base64 = captcha_image_base64.strip()
                captcha_image_base64 = captcha_image_base64.replace('\n', '').replace('\r', '').replace(' ', '')
                
                # Проверяем валидность base64
                try:
                    decoded = base64.b64decode(captcha_image_base64, validate=True)
                    if len(decoded) < 100:  # Минимальный размер изображения
                        self.log_message("❌ Изображение слишком маленькое", "ERROR")
                        return None
                except Exception as e:
                    self.log_message(f"❌ Невалидный base64 формат: {e}", "ERROR")
                    return None
                
                # Формируем запрос
                url = "http://rucaptcha.com/in.php"
                
                # Используем multipart/form-data для отправки
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
                        # Отправляем капчу
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
                                
                                # Ждем решения
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
        """Ожидает решения капчи"""
        try:
            max_wait_time = 120  # Максимальное время ожидания в секундах
            start_time = time.time()
            check_interval = 3  # Интервал проверки в секундах
            
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
        """Отправляет решенную капчу на сайт - улучшенная версия"""
        try:
            self.log_message(f"📤 Отправляем решение капчи: {captcha_text}")
            
            # Ждем немного перед отправкой
            await asyncio.sleep(1)
            
            # Пробуем разные стратегии отправки
            
            # Стратегия 1: Ищем поле ввода и кнопку отправки
            success = await self._submit_captcha_strategy_input_button(page, captcha_text)
            if success:
                return True
            
            # Стратегия 2: Пробуем через JavaScript
            success = await self._submit_captcha_strategy_javascript(page, captcha_text)
            if success:
                return True
            
            # Стратегия 3: Пробуем через API
            success = await self._submit_captcha_strategy_api(page, captcha_text)
            if success:
                return True
            
            self.log_message("❌ Не удалось отправить решение капчи ни одним способом", "ERROR")
            return False
            
        except Exception as e:
            self.log_message(f"❌ Ошибка при отправке решения капчи: {e}", "ERROR")
            return False

    async def _submit_captcha_strategy_input_button(self, page: Page, captcha_text: str) -> bool:
        """Стратегия 1: Ищем поле ввода и кнопку отправки"""
        try:
            # Ищем поле для ввода капчи
            input_selectors = [
                'input[name="captcha"]',
                'input[name="captcha_code"]',
                'input[name="code"]',
                'input[placeholder*="капч"]',
                'input[placeholder*="код"]',
                '#captcha',
                '.captcha-input',
                'input#captcha',
                'input[type="text"][name*="captcha"]',
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
                # Проверяем фреймы
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
            
            # Очищаем и вводим текст
            await captcha_input.fill('')
            await captcha_input.type(captcha_text, delay=100)
            await asyncio.sleep(0.5)
            
            # Ищем кнопку отправки
            button_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Отправить")',
                'button:has-text("Проверить")',
                'button:has-text("Подтвердить")',
                'button:has-text("OK")',
                'button:has-text("Далее")',
                '.submit-button',
                '.captcha-submit',
                'button.btn-primary',
                'button.btn-success',
                'button.btn-submit'
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
                # Кликаем на кнопку
                await submit_button.click()
                await asyncio.sleep(2)
                return True
            else:
                # Пробуем нажать Enter
                await captcha_input.press('Enter')
                await asyncio.sleep(2)
                return True
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в стратегии 1: {e}", "WARNING")
            return False

    async def _submit_captcha_strategy_javascript(self, page: Page, captcha_text: str) -> bool:
        """Стратегия 2: Отправка через JavaScript"""
        try:
            # Пробуем выполнить JavaScript для отправки формы
            result = await page.evaluate("""
                (captchaText) => {
                    try {
                        // Ищем все поля ввода
                        const inputs = document.querySelectorAll('input, textarea');
                        let captchaField = null;
                        
                        // Ищем поле для капчи
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
                        
                        // Устанавливаем значение
                        captchaField.value = captchaText;
                        
                        // Ищем форму
                        let form = captchaField.closest('form');
                        if (!form) {
                            // Ищем любую форму на странице
                            const forms = document.querySelectorAll('form');
                            if (forms.length > 0) {
                                form = forms[0];
                            }
                        }
                        
                        if (form) {
                            // Отправляем форму
                            form.submit();
                            console.log('Форма отправлена через JS');
                            return true;
                        } else {
                            // Если нет формы, ищем кнопку отправки
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
            else:
                return False
                
        except Exception as e:
            self.log_message(f"⚠️ Ошибка в стратегии 2: {e}", "WARNING")
            return False

    async def _submit_captcha_strategy_api(self, page: Page, captcha_text: str) -> bool:
        """Стратегия 3: Отправка через API (если используется AJAX)"""
        try:
            # Проверяем, есть ли API для отправки капчи
            current_url = page.url
            if '/api/' in current_url or 'ajax' in current_url.lower():
                # Пробуем найти CSRF токен
                csrf_token = await page.evaluate("""
                    () => {
                        return document.querySelector('meta[name="csrf-token"]')?.content || 
                               document.querySelector('input[name="_token"]')?.value ||
                               document.querySelector('input[name="csrf_token"]')?.value;
                    }
                """)
                
                if csrf_token:
                    # Формируем данные для отправки
                    form_data = {
                        'captcha': captcha_text,
                        '_token': csrf_token
                    }
                    
                    # Отправляем POST запрос
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
                                    body: JSON.stringify({
                                        captcha: formData.captcha
                                    })
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
        """Загружает изображение капчи по URL"""
        try:
            # Если URL относительный, добавляем базовый URL
            if url.startswith('/'):
                url = f"{self.base_url}{url}"
            elif url.startswith('./'):
                url = f"{self.base_url}{url[1:]}"
            elif not url.startswith(('http://', 'https://', 'data:')):
                url = f"{self.base_url}/{url}"
            
            self.log_message(f"📥 Загружаем изображение: {url[:100]}...")
            
            # Пробуем несколько способов загрузки
            try:
                # Способ 1: через fetch API страницы
                image_data = await page.evaluate("""
                    async (url) => {
                        try {
                            const response = await fetch(url);
                            if (!response.ok) {
                                throw new Error(`HTTP ${response.status}`);
                            }
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
            
            # Способ 2: через aiohttp
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
        """Обрабатывает капчу на странице - улучшенная версия"""
        try:
            self.log_message("🔄 Проверяем наличие капчи на странице...")
            
            # Проверяем наличие капчи по разным признакам
            captcha_detected = False
            captcha_base64 = None
            
            # 1. Проверяем по тексту на странице (быстрый способ)
            page_content = await page.content()
            captcha_keywords = ['капч', 'captcha', 'код безопасности', 'введите код', 'защита от роботов']
            for keyword in captcha_keywords:
                if keyword.lower() in page_content.lower():
                    self.log_message(f"🔍 Обнаружено ключевое слово капчи: {keyword}")
                    captcha_detected = True
                    break
            
            if not captcha_detected:
                # 2. Проверяем по селекторам
                captcha_selectors = [
                    'img[src*="captcha"]',
                    'img[src*="Captcha"]',
                    'img[src*="CAPTCHA"]',
                    'img.captcha',
                    '.captcha img',
                    '#captcha-image',
                    '.captcha-image',
                    'img[alt*="капч"]',
                    'img[alt*="код"]',
                    'img[title*="капч"]',
                    'img[alt*="captcha"]',
                    'img[title*="captcha"]',
                    'img#captcha',
                    '[class*="captcha"] img',
                    '[id*="captcha"] img'
                ]
                
                # Проверяем все селекторы с увеличением таймаута
                for selector in captcha_selectors:
                    try:
                        element = await page.query_selector(selector)
                        if element:
                            self.log_message(f"✅ Нашли элемент капчи по селектору: {selector}")
                            captcha_detected = True
                            
                            # Получаем src атрибут
                            src = await element.get_attribute('src')
                            if src:
                                if src.startswith('data:image'):
                                    # Извлекаем base64 из data URL
                                    try:
                                        captcha_base64 = src.split('base64,')[1]
                                        self.log_message(f"📊 Длина base64: {len(captcha_base64)} символов")
                                        break
                                    except (IndexError, AttributeError) as e:
                                        self.log_message(f"⚠️ Не удалось извлечь base64: {e}", "WARNING")
                                        # Пробуем получить как обычный src
                                        captcha_base64 = src
                                else:
                                    # Загружаем изображение по URL
                                    self.log_message(f"📥 Загружаем изображение по URL: {src[:100]}...")
                                    captcha_base64 = await self.download_captcha_image(page, src)
                                    
                            # Также проверяем data-атрибуты
                            if not captcha_base64:
                                data_src = await element.get_attribute('data-src')
                                if data_src:
                                    self.log_message(f"📥 Нашли data-src: {data_src[:100]}...")
                                    captcha_base64 = await self.download_captcha_image(page, data_src)
                    except Exception as e:
                        self.log_message(f"⚠️ Ошибка при проверке селектора {selector}: {e}", "WARNING")
                        continue
            
            # 3. Проверяем скрытые поля с капчей
            if not captcha_base64:
                hidden_captcha_selectors = [
                    'input[type="hidden"][name*="captcha"]',
                    'input[type="hidden"][value*="base64"]',
                    'input[name="captcha_image"]',
                    '[data-captcha]',
                    'input#captcha_image',
                    'textarea[name="captcha"]',
                    '.captcha-data',
                    '[data-image]'
                ]
                
                for selector in hidden_captcha_selectors:
                    try:
                        hidden_input = await page.query_selector(selector)
                        if hidden_input:
                            value = await hidden_input.get_attribute('value') or await hidden_input.text_content()
                            if value:
                                self.log_message(f"🔍 Нашли скрытое поле капчи: {selector}")
                                
                                # Проверяем разные форматы
                                if 'base64,' in value:
                                    try:
                                        captcha_base64 = value.split('base64,')[1]
                                        self.log_message(f"📊 Извлекли base64 из скрытого поля, длина: {len(captcha_base64)}")
                                        captcha_detected = True
                                        break
                                    except IndexError:
                                        self.log_message(f"⚠️ Не удалось извлечь base64 из значения", "WARNING")
                                elif 'data:image' in value:
                                    captcha_base64 = value
                                    captcha_detected = True
                                    break
                    except Exception as e:
                        self.log_message(f"⚠️ Ошибка при проверке скрытого поля {selector}: {e}", "WARNING")
                        continue
            
            # 4. Если капча обнаружена, но base64 не получен, делаем скриншот области капчи
            if captcha_detected and not captcha_base64:
                self.log_message("📸 Делаем скриншот области капчи...")
                try:
                    # Ищем контейнер капчи
                    captcha_container_selectors = [
                        '.captcha',
                        '#captcha',
                        '.captcha-container',
                        '[class*="captcha"]',
                        '[id*="captcha"]',
                        '.form-group:has(img[src*="captcha"])',
                        '.row:has(img[src*="captcha"])'
                    ]
                    
                    for selector in captcha_container_selectors:
                        container = await page.query_selector(selector)
                        if container:
                            # Делаем скриншот контейнера
                            screenshot_bytes = await container.screenshot()
                            if screenshot_bytes:
                                captcha_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                                self.log_message(f"📸 Сделали скриншот капчи, длина base64: {len(captcha_base64)}")
                                break
                except Exception as e:
                    self.log_message(f"⚠️ Ошибка при создании скриншота капчи: {e}", "WARNING")
            
            # Если нашли капчу, пытаемся ее решить
            if captcha_detected and captcha_base64:
                self.log_message("🎯 Капча обнаружена, пытаемся решить...")
                
                # Сохраняем капчу для отладки
                try:
                    captcha_filename = os.path.join(self.captchas_folder, 
                                                   f"captcha_{int(time.time())}_{self.thread_id}.png")
                    
                    # Очищаем base64 если нужно
                    if 'base64,' in captcha_base64:
                        captcha_base64 = captcha_base64.split('base64,')[1]
                    
                    # Удаляем возможные пробелы и переносы строк
                    captcha_base64 = captcha_base64.strip()
                    captcha_base64 = captcha_base64.replace('\n', '').replace('\r', '').replace(' ', '')
                    
                    with open(captcha_filename, "wb") as f:
                        f.write(base64.b64decode(captcha_base64))
                    
                    file_size = os.path.getsize(captcha_filename)
                    self.log_message(f"💾 Капча сохранена: {captcha_filename} ({file_size} байт)")
                except Exception as e:
                    self.log_message(f"⚠️ Ошибка сохранения капчи: {e}", "WARNING")
                
                # Решаем капчу
                captcha_text = await self.solve_captcha(captcha_base64)
                if captcha_text:
                    # Отправляем решение
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
        """Ожидание готовности страницы"""
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=timeout)
            await page.wait_for_load_state('networkidle', timeout=timeout)
            self.log_message("Страница загружена")
            return True
            
        except Exception as e:
            self.log_message(f"Ошибка загрузки страницы: {e}", "WARNING")
            return False

    async def navigate_to_main_page(self, page: Page):
        """Переход на главную страницу выбора номера"""
        try:
            self.log_message(f"Переходим на основную страницу: {self.main_url}")
            
            # Применяем задержку перед запросом
            await self.apply_anti_captcha_delay()
            
            # Пробуем несколько стратегий
            strategies = [
                self._try_direct_navigation,
                self._try_with_referer,
                self._try_via_homepage,
                self._try_with_retry
            ]
            
            for strategy in strategies:
                if await strategy(page):
                    # Проверяем наличие капчи на странице
                    if await self.handle_captcha_on_page(page):
                        return True
            
            return False
            
        except Exception as e:
            self.log_message(f"Ошибка перехода на основную страницу: {e}", "ERROR")
            return False

    async def _try_direct_navigation(self, page: Page):
        """Прямой переход"""
        try:
            response = await page.goto(
                self.main_url,
                wait_until='domcontentloaded',
                timeout=30000
            )
            
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
            
        except Exception as e:
            self.log_message(f"Прямой переход не удался: {e}", "WARNING")
            return False

    async def _try_with_referer(self, page: Page):
        """Переход с реферером"""
        try:
            # Сначала загружаем главную страницу
            await page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
            await asyncio.sleep(2)
            
            # Затем переходим на целевую страницу
            response = await page.goto(
                self.main_url,
                wait_until='domcontentloaded',
                timeout=30000,
                referer=self.base_url
            )
            
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
            
        except Exception as e:
            self.log_message(f"Переход с реферером не удался: {e}", "WARNING")
            return False

    async def _try_via_homepage(self, page: Page):
        """Переход через домашнюю страницу"""
        try:
            # Сначала на домашнюю страницу
            response = await page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
            if not response or response.status != 200:
                return False
                
            await asyncio.sleep(2)
            
            # Ищем ссылку на выбор номера
            link_selectors = [
                'a[href*="chnumber"]',
                'a[href*="lnumber"]',
                'a:has-text("Выбрать номер")',
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
        """Повторная попытка с задержкой"""
        try:
            # Ждем и пробуем снова
            await asyncio.sleep(5)
            
            response = await page.goto(
                self.main_url,
                wait_until='domcontentloaded',
                timeout=40000
            )
            
            if response and response.status == 200:
                await self.wait_for_page_ready(page)
                return True
            return False
            
        except Exception as e:
            self.log_message(f"Повторный переход не удался: {e}", "WARNING")
            return False

    async def enter_combination_and_search_numbers(self, page: Page, combination: str) -> bool:
        """Вводит комбинацию и ищет номера (ОСНОВНАЯ ФУНКЦИЯ)"""
        try:
            self.log_message(f"🔍 Ищем номера по комбинации: {combination}")
            
            # Проверяем наличие капчи перед вводом
            if not await self.handle_captcha_on_page(page):
                self.log_message("❌ Не удалось обойти капчу перед вводом комбинации", "ERROR")
                return False
            
            # Применяем задержку перед вводом
            await self.apply_anti_captcha_delay()
            
            # Ищем поле для ввода разными способами
            input_selectors = [
                '#favoriteNumber',
                'input[name="favoriteNumber"]',
                'input[type="tel"]',
                'input.search-input',
                '.phone-input',
                'input[placeholder*="номер"]',
                'input[placeholder*="цифр"]',
                f'input[placeholder*="{self.digits_count} цифр"]',
                f'input[placeholder*="{self.digits_count} цифры"]',
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
                # Делаем скриншот для отладки
                screenshot_path = os.path.join(self.screenshots_folder, f"debug_{int(time.time())}_{self.thread_id}.png")
                await page.screenshot(path=screenshot_path)
                self.log_message(f"📸 Сделан скриншот для отладки: {screenshot_path}")
                return False
            
            # Очищаем и вводим комбинацию
            await input_field.fill('')
            await input_field.type(combination, delay=random.uniform(50, 150))  # Случайная задержка между нажатиями
            
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            # Ищем кнопку поиска
            search_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button.search-button',
                '.search-btn',
                'button:has-text("Найти")',
                'button:has-text("Поиск")',
                'button:has-text("Поиск номера")',
                'button:has-text("🔍")',
                'button:has-text("Поиск по цифрам")',
                'button.btn-primary',
                'button.btn-search'
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
                # Если нет кнопки, пробуем нажать Enter
                self.log_message("Кнопка поиска не найдена, пробуем Enter...")
                await input_field.press('Enter')
            else:
                # Кликаем с небольшой случайной задержкой
                await asyncio.sleep(random.uniform(0.3, 0.7))
                await search_button.click()
            
            # Ждем результаты поиска с таймаутом
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except:
                self.log_message("⚠️ Таймаут ожидания загрузки результатов", "WARNING")
            
            # Добавляем дополнительную задержку для появления результатов
            await asyncio.sleep(3)
            
            # Проверяем наличие результатов на странице
            result_selectors = [
                '.phone-numbers',
                '.numbers-list',
                '.numbers-grid',
                '.msisdn-list',
                '[data-type="phone"]',
                '.number-item',
                '.phone-item',
                '.phone-number',
                '.phone-number-item',
                '.catalog-numbers__item',
                '.catalog-item'
            ]
            
            has_results = False
            for selector in result_selectors:
                results = await page.query_selector_all(selector)
                if results and len(results) > 0:
                    self.log_message(f"Нашли {len(results)} номеров по селектору: {selector}")
                    has_results = True
                    
                    # Извлекаем номера
                    await self.extract_numbers_from_page(page, combination)
                    break
            
            if not has_results:
                # Пробуем найти номера в тексте страницы
                content = await page.content()
                phone_patterns = [
                    r'\+7\d{10}',  # +7XXXXXXXXXX
                    r'7\d{10}',    # 7XXXXXXXXXX
                    r'8\d{10}',    # 8XXXXXXXXXX
                    r'\d{11}'      # XXXXXXXXXXX
                ]
                
                for pattern in phone_patterns:
                    phones = re.findall(pattern, content)
                    if phones:
                        self.log_message(f"Нашли {len(phones)} номеров по паттерну")
                        for phone in phones:
                            # Нормализуем номер
                            if phone.startswith('+7'):
                                phone_str = phone[2:]  # Убираем +7
                            elif phone.startswith('7'):
                                phone_str = phone[1:]  # Убираем 7
                            elif phone.startswith('8'):
                                phone_str = phone[1:]  # Убираем 8
                            else:
                                phone_str = phone
                            
                            if len(phone_str) == 10:
                                self.process_found_number(phone_str, combination)
                        break
            
            # Проверяем капчу после поиска
            await self.handle_captcha_on_page(page)
            
            return True
            
        except Exception as e:
            self.log_message(f"❌ Ошибка при поиске номеров: {e}", "ERROR")
            return False

    async def extract_numbers_from_page(self, page: Page, combination: str):
        """Извлекает номера со страницы результатов"""
        try:
            # Ищем элементы с номерами
            number_elements = await page.query_selector_all('.phone-number, .number, .msisdn, [data-phone], [data-number], .catalog-numbers__phone')
            
            for element in number_elements[:100]:  # Увеличиваем лимит
                # Получаем текст элемента
                text = await element.text_content()
                if text:
                    # Ищем номер в тексте
                    phone_match = re.search(r'\d{10,11}', text)
                    if phone_match:
                        phone_str = phone_match.group()
                        if len(phone_str) == 11:
                            # Преобразуем 8XXXXXXXXXX или 7XXXXXXXXXX в XXXXXXXXXX
                            if phone_str.startswith('7'):
                                phone_str = phone_str[1:]
                            elif phone_str.startswith('8'):
                                phone_str = phone_str[1:]
                        
                        if len(phone_str) == 10:
                            self.process_found_number(phone_str, combination)
            
            # Также пробуем извлечь из data-атрибутов
            phone_elements = await page.query_selector_all('[data-phone], [data-number], [data-msisdn]')
            for element in phone_elements:
                phone_attr = await element.get_attribute('data-phone') or await element.get_attribute('data-number') or await element.get_attribute('data-msisdn')
                if phone_attr:
                    phone_str = re.sub(r'\D', '', phone_attr)
                    if len(phone_str) == 10:
                        self.process_found_number(phone_str, combination)
                    
        except Exception as e:
            self.log_message(f"⚠️ Ошибка извлечения номеров: {e}", "WARNING")

    def process_found_number(self, phone_str: str, combination: str):
        """Обрабатывает найденный номер"""
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
        """Настраивает браузер с поддержкой прокси"""
        if not self.is_running:
            return None, None, None, None

        max_retries = 5  # Увеличиваем количество попыток
        for attempt in range(max_retries):
            try:
                self.playwright = await async_playwright().start()

                # Базовые аргументы запуска
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
                
                # Добавляем дополнительные настройки для стабильности
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

                launch_options = {
                    'headless': True,
                    'args': args,
                    'timeout': 60000
                }

                if self.proxy_config and self.proxy_config.get('server'):
                    proxy_server = self.proxy_config['server']
                    
                    # Определяем тип прокси из URL
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
                    
                    # Формируем прокси для Playwright
                    proxy_for_playwright = {
                        'server': proxy_server
                    }
                    
                    # Добавляем аутентификацию если есть
                    if self.proxy_config.get('username') and self.proxy_config.get('password'):
                        proxy_for_playwright['username'] = self.proxy_config['username']
                        proxy_for_playwright['password'] = self.proxy_config['password']
                    
                    launch_options['proxy'] = proxy_for_playwright
                    
                    # Для SOCKS прокси добавляем дополнительные аргументы
                    if proxy_type in ['socks4', 'socks5']:
                        launch_options['args'].extend([
                            f'--proxy-server={proxy_server_clean}',
                            '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE localhost'
                        ])
                    
                    self.log_message(f"🔄 Используется прокси: {proxy_server} (тип: {proxy_type})")
                else:
                    self.log_message("⚠️ Работаем без прокси")

                self.browser = await self.playwright.chromium.launch(**launch_options)

                # Настройки контекста
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

                # Добавляем прокси в контекст если есть
                if self.proxy_config and self.proxy_config.get('server'):
                    context_options['proxy'] = launch_options['proxy']

                self.context = await self.browser.new_context(**context_options)

                # Антидетект
                await self.context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    });
                    
                    // Chrome только
                    window.chrome = {
                        runtime: {},
                        loadTimes: function() {},
                        csi: function() {},
                        app: {
                            isInstalled: false,
                            InstallState: {
                                DISABLED: 'disabled',
                                INSTALLED: 'installed',
                                NOT_INSTALLED: 'not_installed'
                            },
                            RunningState: {
                                CANNOT_RUN: 'cannot_run',
                                READY_TO_RUN: 'ready_to_run',
                                RUNNING: 'running'
                            }
                        }
                    };
                    
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5],
                    });
                    
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['ru-RU', 'ru', 'en-US', 'en'],
                    });
                    
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                    
                    // Pass the iframe test
                    window.__chromedriver = false;
                """)

                self.page = await self.context.new_page()
                
                # Устанавливаем таймауты для страницы
                self.page.set_default_timeout(60000)
                self.page.set_default_navigation_timeout(60000)

                self.log_message("✅ Браузер успешно запущен")
                return self.playwright, self.browser, self.context, self.page

            except Exception as e:
                error_msg = str(e)
                self.log_message(f"❌ Попытка {attempt + 1}/{max_retries} запуска браузера не удалась: {error_msg}", "ERROR")
                
                # Очищаем ресурсы перед следующей попыткой
                await self.cleanup_browser()
                
                # Если это ошибка прокси, пробуем без прокси
                if any(proxy_error in error_msg.lower() for proxy_error in ['proxy', 'tunnel', 'connection failed', 'timeout', 'connection closed', 'err_connection_closed']):
                    self.log_message("⚠️ Проблема с прокси, пробуем без прокси...", "WARNING")
                    self.proxy_config = None
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))  # Экспоненциальная задержка
                else:
                    self.log_message("❌ Не удалось запустить браузер после нескольких попыток", "ERROR")
                    return None, None, None, None

        return None, None, None, None

    async def handle_api_response(self, response: Response):
        """Обработчик API ответов"""
        if self.api_url in response.url:
            try:
                data = await response.json()
                self.stats['requests_made'] += 1
                self.stats['last_activity'] = datetime.now().strftime("%H:%M:%S")

                if data.get('success') is False and data.get('errors'):
                    for error in data.get('errors', []):
                        if any(keyword in error.get('message', '').lower() for keyword in
                               ['капч', 'captcha', 'код', 'code']):
                            self.log_message("Обнаружена капча")
                            captcha_image = data.get('payload', {}).get('captcha', '')
                            if captcha_image:
                                self.captcha_detected = True
                                self.current_captcha_image = captcha_image
                                
                                # Пробуем решить капчу
                                captcha_text = await self.solve_captcha(captcha_image)
                                if captcha_text and self.page:
                                    # Нужно отправить решение капчи через API
                                    await self.submit_api_captcha_solution(captcha_text)
                            return

                # Парсим номера из API ответа
                await self.parse_numbers_from_api(data)

            except Exception as e:
                self.log_message(f"Ошибка обработки API: {e}", "ERROR")
                self.stats['errors'] += 1

    async def parse_numbers_from_api(self, data: Dict[str, Any]):
        """Парсит номера из API ответа"""
        try:
            # Проверяем различные возможные структуры ответа
            numbers_found = False
            
            # Структура 1: data -> regular -> numbers
            if 'regular' in data and isinstance(data['regular'], dict):
                numbers_data = data['regular'].get('numbers', [])
                if isinstance(numbers_data, list):
                    for number_group in numbers_data:
                        if isinstance(number_group, dict) and 'phones' in number_group:
                            phones = number_group.get('phones', [])
                            for phone in phones:
                                phone_str = str(phone)
                                if len(phone_str) >= 10:
                                    # Берем последние 10 цифр
                                    if len(phone_str) > 10:
                                        phone_str = phone_str[-10:]
                                    self.process_found_number(phone_str, self.current_combination)
                                    numbers_found = True
            
            # Структура 2: data -> numbers
            elif 'numbers' in data and isinstance(data['numbers'], list):
                for number_item in data['numbers']:
                    if isinstance(number_item, dict) and 'phone' in number_item:
                        phone_str = str(number_item['phone'])
                        if len(phone_str) >= 10:
                            if len(phone_str) > 10:
                                phone_str = phone_str[-10:]
                            self.process_found_number(phone_str, self.current_combination)
                            numbers_found = True
            
            # Структура 3: data -> list
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
        """Отправляет решение капчи через API"""
        try:
            # Формируем URL для отправки капчи
            captcha_url = f"{self.api_url}/captcha"
            
            # Получаем CSRF токен если есть
            csrf_token = await self.page.evaluate('''() => {
                return document.querySelector('meta[name="csrf-token"]')?.content || 
                       document.querySelector('input[name="_token"]')?.value;
            }''')
            
            headers = {
                'Content-Type': 'application/json',
                'X-Requested-With': 'XMLHttpRequest'
            }
            
            if csrf_token:
                headers['X-CSRF-TOKEN'] = csrf_token
            
            payload = {
                'captcha': captcha_text,
                'favoriteNumber': self.current_combination
            }
            
            # Отправляем запрос
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
        """Выполняет цикл поиска для одной комбинации"""
        try:
            # Получаем следующую комбинации
            combination = self.get_next_combination()
            if not combination:
                self.log_message("Комбинации закончились", "WARNING")
                return False

            self.log_message(f"🔍 Начинаем обработку комбинации: {combination}")
            
            # Переходим на главную страницу
            if not await self.navigate_to_main_page(page):
                self.log_message("❌ Не удалось перейти на главную страницу", "ERROR")
                return False

            # Вводим комбинацию и ищем номера
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
        """Основной цикл парсера"""
        self.stats['status'] = 'Запущен'

        while self.is_running:
            try:
                self.log_message(f"🚀 Начало нового цикла")
                self.log_message(f"📍 Регион: {self.region}")
                self.log_message(f"📊 Номеров: {len(self.found_numbers)}, Общих: {len(self.shared_numbers)}")
                self.log_message(f"🔢 Комбинаций обработано: {self.stats['combinations_tried']}")
                self.log_message(f"🔢 Поиск по {self.digits_count} цифрам")

                # Запускаем браузер
                playwright, browser, context, page = await self.setup_browser()
                if not all([playwright, browser, context, page]):
                    self.log_message("❌ Не удалось запустить браузер", "ERROR")
                    await asyncio.sleep(10)  # Увеличиваем задержку
                    continue

                # Настраиваем обработчик ответов
                page.on('response', self.handle_api_response)

                try:
                    # Выполняем циклы поиска
                    search_count = 0
                    while self.is_running and search_count < 10:  # 10 комбинаций за сессию
                        search_count += 1

                        if not await self.perform_search_cycle(page):
                            self.log_message("🔄 Пробуем следующую комбинацию...")
                            continue

                        # Пауза между комбинациями с применением антикапча-задержки
                        await self.apply_anti_captcha_delay()
                        
                        # Дополнительная случайная задержка
                        extra_delay = random.uniform(2, 5)
                        await asyncio.sleep(extra_delay)

                except Exception as e:
                    self.log_message(f"❌ Ошибка в сессии: {e}", "ERROR")
                    self.stats['errors'] += 1

                finally:
                    await self.cleanup_browser()

                # Большая пауза между сессиями для предотвращения капчи
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
        """Очистка ресурсов"""
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
        """Запуск парсера"""
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
        """Остановка парсера"""
        self.is_running = False
        self.stats['status'] = 'Останавливается'
        self.log_message("🛑 Получена команда остановки...")
        
        # Не запускаем асинхронную очистку, чтобы не создавать новый цикл событий
        # Вместо этого просто сбрасываем флаги
        
        self.log_message("✅ Парсер остановлен")

    def get_stats(self) -> Dict:
        """Возвращает статистику"""
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


class MegafonParserGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Megafon Parser - Поиск по любимому номеру")
        self.root.geometry("1200x800")
        self.root.configure(bg='#2b2b2b')
        
        self.setup_styles()
        
        self.parser = None
        self.threads = []
        self.proxies = []
        self.bad_proxies = []  # Список нерабочих прокси
        self.current_proxy_index = 0
        self.parsers = []
        self.total_numbers_found = 0
        self.base_folder = "megafon_parser_results"
        
        self.shared_numbers = set()
        self.shared_duplicates = {}
        
        # Файл для хранения нерабочих прокси
        self.bad_proxies_file = os.path.join(self.base_folder, "bad_proxies.txt")

        self.setup_ui()

    def setup_styles(self):
        """Настраивает стили"""
        style = ttk.Style()
        style.theme_use('clam')
        
        style.configure('TFrame', background='#2b2b2b')
        style.configure('TLabel', background='#2b2b2b', foreground='white')
        style.configure('TButton', background='#404040', foreground='white')
        style.configure('TLabelframe', background='#2b2b2b', foreground='white')
        style.configure('TLabelframe.Label', background='#2b2b2b', foreground='#4fc3f7')
        style.configure('TEntry', fieldbackground='#404040', foreground='white')
        style.configure('TSpinbox', fieldbackground='#404040', foreground='white')
        style.configure('TScrollbar', background='#404040')
        style.configure('Accent.TButton', background='#007acc', foreground='white')
        
        self.root.option_add('*Text.Background', '#1e1e1e')
        self.root.option_add('*Text.Foreground', 'white')
        self.root.option_add('*Text.InsertBackground', 'white')

    def setup_ui(self):
        """Создает интерфейс"""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # Заголовок
        header_frame = ttk.Frame(main_frame)
        header_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=(0, 10))
        
        ttk.Label(header_frame, text="Megafon Parser - Поиск по любимому номеру", 
                 font=('Arial', 16, 'bold'), foreground='#4fc3f7').pack(side=tk.LEFT)
        
        self.counter_var = tk.StringVar(value="📞 Всего номеров: 0")
        self.counter_label = ttk.Label(header_frame, textvariable=self.counter_var, 
                                      font=('Arial', 14, 'bold'), foreground='#00ff00')
        self.counter_label.pack(side=tk.RIGHT)

        # Настройки
        settings_frame = ttk.LabelFrame(main_frame, text="Настройки", padding="10")
        settings_frame.grid(row=1, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        row1 = ttk.Frame(settings_frame)
        row1.grid(row=0, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(row1, text="RuCaptcha API ключ:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.api_key_var = tk.StringVar(value="dcfc7131e39d1c8671156feaaedbf1f6")
        ttk.Entry(row1, textvariable=self.api_key_var, width=30).grid(row=0, column=1, padx=(0, 20))

        ttk.Label(row1, text="Потоки:").grid(row=0, column=2, sticky=tk.W, padx=(0, 10))
        self.threads_var = tk.IntVar(value=1)
        ttk.Spinbox(row1, from_=1, to=10, textvariable=self.threads_var, width=5).grid(row=0, column=3, padx=(0, 20))

        row2 = ttk.Frame(settings_frame)
        row2.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(row2, text="Регион:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.region_var = tk.StringVar(value="moscow")
        ttk.Entry(row2, textvariable=self.region_var, width=20).grid(row=0, column=1, padx=(0, 20))

        ttk.Label(row2, text="Цифр для поиска:").grid(row=0, column=2, sticky=tk.W, padx=(0, 10))
        self.digits_var = tk.IntVar(value=4)
        ttk.Spinbox(row2, from_=1, to=4, textvariable=self.digits_var, width=5).grid(row=0, column=3, padx=(0, 20))

        row3 = ttk.Frame(settings_frame)
        row3.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(row3, text="Файл прокси:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.proxy_file_var = tk.StringVar(value="proxies.txt")
        ttk.Entry(row3, textvariable=self.proxy_file_var, width=20).grid(row=0, column=1, padx=(0, 10))
        ttk.Button(row3, text="Обзор", command=self.browse_proxy_file, width=8).grid(row=0, column=2)
        ttk.Button(row3, text="Конвертировать", command=self.convert_proxy_file, width=12).grid(row=0, column=3)

        # Настройки задержки
        row4 = ttk.Frame(settings_frame)
        row4.grid(row=3, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(row4, text="Мин. задержка (сек):").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.min_delay_var = tk.IntVar(value=10)
        ttk.Spinbox(row4, from_=5, to=60, textvariable=self.min_delay_var, width=10).grid(row=0, column=1, padx=(0, 20))

        ttk.Label(row4, text="Макс. задержка (сек):").grid(row=0, column=2, sticky=tk.W, padx=(0, 10))
        self.max_delay_var = tk.IntVar(value=30)
        ttk.Spinbox(row4, from_=10, to=120, textvariable=self.max_delay_var, width=10).grid(row=0, column=3, padx=(0, 20))

        # Прокси управление
        row5 = ttk.Frame(settings_frame)
        row5.grid(row=4, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(row5, text="Управление прокси:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        
        self.test_proxy_button = ttk.Button(row5, text="Проверить прокси", command=self.test_proxies, width=15)
        self.test_proxy_button.grid(row=0, column=1, padx=(0, 10))
        
        self.remove_bad_proxy_button = ttk.Button(row5, text="Удалить нерабочие", command=self.remove_bad_proxies, width=15)
        self.remove_bad_proxy_button.grid(row=0, column=2, padx=(0, 10))
        
        self.proxy_status_var = tk.StringVar(value="Статус: не проверен")
        ttk.Label(row5, textvariable=self.proxy_status_var, foreground='#ff9900').grid(row=0, column=3, padx=(0, 10))

        # Кнопки
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=5, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=10)

        self.start_button = ttk.Button(button_frame, text="▶ Запуск", command=self.start_parsing, 
                                      style='Accent.TButton')
        self.start_button.pack(side=tk.LEFT, padx=(0, 10))

        self.stop_button = ttk.Button(button_frame, text="⏹ Стоп", command=self.stop_parsing, 
                                     state='disabled')
        self.stop_button.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Button(button_frame, text="🗑 Очистить логи", command=self.clear_logs).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(button_frame, text="💾 Сохранить номера", command=self.save_numbers).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(button_frame, text="🔄 Обновить прокси", command=self.load_proxies).pack(side=tk.LEFT)

        # Статистика
        stats_frame = ttk.LabelFrame(main_frame, text="📊 Статистика", padding="10")
        stats_frame.grid(row=6, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        self.stats_text = tk.Text(stats_frame, height=8, width=80, bg='#1e1e1e', fg='white', 
                                 font=('Consolas', 9))
        self.stats_text.pack(fill=tk.BOTH, expand=True)

        # Логи
        log_frame = ttk.LabelFrame(main_frame, text="📝 Логи", padding="10")
        log_frame.grid(row=7, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=80, bg='#1e1e1e', 
                                                fg='white', font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Настройка растягивания
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(7, weight=1)

        # Загружаем прокси
        self.load_proxies()
        self.update_stats()

    def normalize_proxy_format(self, proxy_str: str) -> str:
        """Нормализует формат прокси-строки"""
        proxy_str = proxy_str.strip()
        
        # Если строка уже содержит протокол, возвращаем как есть
        if proxy_str.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
            return proxy_str
        
        # Проверяем, есть ли аутентификация в нестандартном формате
        if 'логин:' in proxy_str.lower() or 'пароль:' in proxy_str.lower() or 'login:' in proxy_str.lower() or 'password:' in proxy_str.lower():
            # Пример: "147.45.205.116:11223 логин: ztD56HJMkr Пароль:RS5SSgasb7"
            parts = proxy_str.split()
            
            # Извлекаем адрес (первая часть)
            address = parts[0]
            
            # Ищем логин и пароль
            login = None
            password = None
            
            for i, part in enumerate(parts):
                if ':' in part:
                    key, value = part.split(':', 1)
                    key_lower = key.lower()
                    if key_lower in ['логин', 'login']:
                        login = value.strip()
                    elif key_lower in ['пароль', 'password']:
                        password = value.strip()
            
            if login and password:
                return f"http://{login}:{password}@{address}"
            elif login:
                return f"http://{login}@{address}"
            else:
                return f"http://{address}"
        else:
            # Простой адрес без аутентификации
            if '://' not in proxy_str:
                return f"http://{proxy_str}"
            else:
                return proxy_str

    def browse_proxy_file(self):
        """Выбор файла с прокси"""
        filename = filedialog.askopenfilename(
            title="Выберите файл с прокси",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if filename:
            self.proxy_file_var.set(filename)
            self.load_proxies()

    async def test_proxy_async(self, proxy_str: str) -> bool:
        """Асинхронная проверка работоспособности прокси"""
        try:
            proxy_config = self.parse_proxy_string(proxy_str)
            if not proxy_config:
                return False
            
            playwright = await async_playwright().start()
            
            launch_options = {
                'headless': True,
                'args': [
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage'
                ],
                'timeout': 15000
            }
            
            if proxy_config.get('server'):
                launch_options['proxy'] = {
                    'server': proxy_config['server']
                }
                if proxy_config.get('username') and proxy_config.get('password'):
                    launch_options['proxy']['username'] = proxy_config['username']
                    launch_options['proxy']['password'] = proxy_config['password']
            
            browser = await playwright.chromium.launch(**launch_options)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            
            try:
                # Пробуем загрузить тестовую страницу
                response = await page.goto("http://httpbin.org/ip", timeout=15000, wait_until='domcontentloaded')
                if response and response.status == 200:
                    content = await page.content()
                    if 'origin' in content:
                        return True
                return False
            except Exception as e:
                return False
            finally:
                await browser.close()
                await playwright.stop()
                
        except Exception as e:
            return False

    def test_proxies(self):
        """Тестирует загруженные прокси"""
        if not self.proxies:
            self.log_message("❌ Нет прокси для тестирования", "ERROR")
            return
        
        self.test_proxy_button.config(state='disabled')
        self.remove_bad_proxy_button.config(state='disabled')
        self.proxy_status_var.set("Статус: тестирование...")
        
        def run_test():
            working_proxies = []
            bad_proxies = []
            total = len(self.proxies)
            
            for i, proxy in enumerate(self.proxies):
                try:
                    # Создаем новый event loop для теста
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                    result = loop.run_until_complete(self.test_proxy_async(proxy))
                    loop.close()
                    
                    if result:
                        working_proxies.append(proxy)
                        self.root.after(0, lambda p=proxy: self.log_message(f"✅ Прокси {p} рабочий", "SUCCESS"))
                    else:
                        bad_proxies.append(proxy)
                        self.root.after(0, lambda p=proxy: self.log_message(f"❌ Прокси {p} не работает", "WARNING"))
                    
                    # Обновляем статус каждые 10 прокси
                    if i % 10 == 0 or i == total - 1:
                        self.root.after(0, lambda i=i: self.proxy_status_var.set(f"Статус: проверено {i+1}/{total}"))
                        
                except Exception as e:
                    bad_proxies.append(proxy)
                    self.root.after(0, lambda p=proxy, e=e: self.log_message(f"❌ Ошибка тестирования прокси {p}: {e}", "ERROR"))
            
            self.root.after(0, lambda: self.update_proxy_status(working_proxies, bad_proxies, total))
        
        threading.Thread(target=run_test, daemon=True).start()

    def update_proxy_status(self, working_proxies, bad_proxies, total):
        """Обновляет статус прокси"""
        self.test_proxy_button.config(state='normal')
        self.remove_bad_proxy_button.config(state='normal')
        
        # Сохраняем нерабочие прокси
        self.bad_proxies = bad_proxies
        
        if working_proxies:
            self.proxy_status_var.set(f"Статус: {len(working_proxies)}/{total} рабочих")
            self.log_message(f"📊 Рабочих прокси: {len(working_proxies)} из {total}", "SUCCESS")
            self.log_message(f"⚠️ Нерабочих прокси: {len(bad_proxies)}", "WARNING")
        else:
            self.proxy_status_var.set("Статус: нет рабочих прокси")
            self.log_message("⚠️ Нет рабочих прокси. Рекомендуется обновить список.", "WARNING")

    def remove_bad_proxies(self):
        """Удаляет нерабочие прокси из файла"""
        if not self.bad_proxies:
            self.log_message("ℹ️ Нет нерабочих прокси для удаления", "INFO")
            return
        
        proxy_file = self.proxy_file_var.get()
        if not os.path.exists(proxy_file):
            self.log_message("❌ Файл с прокси не найден", "ERROR")
            return
        
        try:
            # Читаем все прокси
            with open(proxy_file, 'r', encoding='utf-8') as f:
                all_proxies = f.read().splitlines()
            
            # Удаляем нерабочие прокси
            initial_count = len(all_proxies)
            working_proxies = []
            
            for proxy in all_proxies:
                proxy_clean = proxy.strip()
                if proxy_clean and not proxy_clean.startswith('#'):
                    # Проверяем, не является ли прокси нерабочим
                    normalized_proxy = self.normalize_proxy_format(proxy_clean)
                    # Сравниваем с нормализованными версиями плохих прокси
                    bad_proxies_normalized = [self.normalize_proxy_format(bp) for bp in self.bad_proxies]
                    if normalized_proxy not in bad_proxies_normalized:
                        working_proxies.append(proxy)
                else:
                    # Сохраняем комментарии и пустые строки
                    working_proxies.append(proxy)
            
            # Сохраняем нерабочие прокси в отдельный файл
            bad_proxies_file = os.path.join(os.path.dirname(proxy_file), "bad_proxies.txt")
            with open(bad_proxies_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n# Нерабочие прокси (удалены {timestamp})\n")
                for bad_proxy in self.bad_proxies:
                    f.write(f"{bad_proxy}\n")
            
            # Записываем рабочие прокси обратно в файл
            with open(proxy_file, 'w', encoding='utf-8') as f:
                f.write('\n'.join(working_proxies))
            
            removed_count = initial_count - len(working_proxies)
            
            # Обновляем список прокси
            self.proxies = []
            for proxy in working_proxies:
                proxy_clean = proxy.strip()
                if proxy_clean and not proxy_clean.startswith('#'):
                    normalized_proxy = self.normalize_proxy_format(proxy_clean)
                    self.proxies.append(normalized_proxy)
            
            self.bad_proxies = []
            
            self.log_message(f"✅ Удалено {removed_count} нерабочих прокси", "SUCCESS")
            self.log_message(f"📊 Осталось {len(self.proxies)} рабочих прокси", "SUCCESS")
            self.log_message(f"💾 Нерабочие прокси сохранены в файл: {bad_proxies_file}", "INFO")
            self.proxy_status_var.set(f"Статус: {len(self.proxies)} рабочих прокси")
            
        except Exception as e:
            self.log_message(f"❌ Ошибка удаления нерабочих прокси: {e}", "ERROR")

    def load_proxies(self):
        """Загружает прокси из файла и нормализует формат"""
        proxy_file = self.proxy_file_var.get()
        self.proxies = []
        
        try:
            if os.path.exists(proxy_file):
                with open(proxy_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            # Нормализуем формат прокси
                            normalized_proxy = self.normalize_proxy_format(line)
                            self.proxies.append(normalized_proxy)
                
                self.log_message(f"📥 Загружено {len(self.proxies)} прокси из файла {proxy_file}", "SUCCESS")
                # Показываем первые 3 прокси в нормализованном формате
                sample_proxies = self.proxies[:3]
                if sample_proxies:
                    self.log_message(f"📋 Примеры загруженных прокси:")
                    for proxy in sample_proxies:
                        # Маскируем пароли в логах
                        masked_proxy = re.sub(r'(?<=://)[^:@]+:[^@]+@', '***:***@', proxy)
                        self.log_message(f"  → {masked_proxy}")
                self.proxy_status_var.set(f"Статус: загружено {len(self.proxies)} прокси")
            else:
                self.log_message("Файл с прокси не найден", "WARNING")
                self.proxy_status_var.set("Статус: файл не найден")
        except Exception as e:
            self.log_message(f"❌ Ошибка загрузки прокси: {e}", "ERROR")
            self.proxy_status_var.set("Статус: ошибка загрузки")

    def convert_proxy_file(self):
        """Конвертирует файл прокси в правильный формат"""
        input_file = filedialog.askopenfilename(
            title="Выберите файл с прокси для конвертации",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        
        if not input_file:
            return
        
        output_file = os.path.join(os.path.dirname(input_file), "proxies_converted.txt")
        
        try:
            converted_count = 0
            with open(input_file, 'r', encoding='utf-8') as f_in, \
                 open(output_file, 'w', encoding='utf-8') as f_out:
                
                f_out.write("# Конвертированные прокси\n")
                f_out.write(f"# Исходный файл: {os.path.basename(input_file)}\n")
                f_out.write(f"# Дата конвертации: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                
                for line in f_in:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        normalized = self.normalize_proxy_format(line)
                        f_out.write(f"{normalized}\n")
                        converted_count += 1
                    else:
                        # Сохраняем комментарии и пустые строки
                        f_out.write(f"{line}\n")
            
            self.log_message(f"✅ Конвертировано {converted_count} прокси", "SUCCESS")
            self.log_message(f"💾 Результат сохранен в: {output_file}", "SUCCESS")
            
            # Автоматически загружаем конвертированные прокси
            self.proxy_file_var.set(output_file)
            self.load_proxies()
            
        except Exception as e:
            self.log_message(f"❌ Ошибка конвертации: {e}", "ERROR")

    def parse_proxy_string(self, proxy_str: str) -> Optional[Dict]:
        """Парсит строку прокси в конфигурацию"""
        try:
            # Форматы прокси:
            # 1. http://84.39.112.144:3128
            # 2. socks4://142.54.239.1:4145
            # 3. socks5://152.228.212.223:29272
            # 4. http://user:pass@84.39.112.144:3128
            # 5. socks5://user:pass@152.228.212.223:29272
            
            # Проверяем, есть ли аутентификация в формате user:pass@host:port
            if '@' in proxy_str:
                # Разделяем на аутентификацию и сервер
                auth_part, server_part = proxy_str.split('@', 1)
                
                # Извлекаем протокол
                if '://' in auth_part:
                    protocol, auth_credentials = auth_part.split('://', 1)
                else:
                    protocol = 'http'
                    auth_credentials = auth_part
                
                # Извлекаем логин и пароль
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
                # Прокси без аутентификации
                # Проверяем наличие протокола
                if '://' in proxy_str:
                    # Уже есть протокол
                    return {'server': proxy_str}
                else:
                    # Нет протокола - предполагаем http
                    return {'server': f'http://{proxy_str}'}
            
        except Exception as e:
            self.log_message(f"❌ Ошибка парсинга прокси {proxy_str}: {e}", "ERROR")
            return None

    def get_proxy_config(self, thread_id: int) -> Optional[Dict]:
        """Возвращает конфигурацию прокси для потока с автоматическим пропуском нерабочих"""
        if not self.proxies:
            return None
        
        # Ищем рабочий прокси (пропускаем нерабочие)
        max_attempts = min(len(self.proxies), 10)  # Пробуем максимум 10 разных прокси
        
        for attempt in range(max_attempts):
            proxy_index = (thread_id + attempt) % len(self.proxies)
            proxy_str = self.proxies[proxy_index]
            
            # Проверяем, не является ли этот прокси нерабочим
            proxy_normalized = self.normalize_proxy_format(proxy_str)
            bad_proxies_normalized = [self.normalize_proxy_format(bp) for bp in self.bad_proxies]
            
            if proxy_normalized in bad_proxies_normalized:
                self.log_message(f"⚠️ Пропускаем нерабочий прокси: {proxy_str}", "WARNING")
                continue
            
            proxy_config = self.parse_proxy_string(proxy_str)
            if proxy_config:
                return proxy_config
        
        # Если все прокси оказались нерабочими
        self.log_message(f"⚠️ Для потока {thread_id} не найдено рабочих прокси", "WARNING")
        return None

    def log_message(self, message: str, level: str = "INFO"):
        """Добавляет сообщение в лог"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        colors = {
            "INFO": "#ffffff",
            "WARNING": "#ffa500",
            "ERROR": "#ff4444",
            "SUCCESS": "#44ff44"
        }
        
        color = colors.get(level, "#ffffff")
        
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.tag_add(level, "end-2l", "end-1l")
        self.log_text.tag_config(level, foreground=color)
        self.log_text.see(tk.END)

    def clear_logs(self):
        """Очищает логи"""
        self.log_text.delete(1.0, tk.END)

    def start_parsing(self):
        """Запускает парсеры"""
        if not self.api_key_var.get() or self.api_key_var.get() == "ВАШ_RUCAPTCHA_API_KEY":
            messagebox.showerror("Ошибка", "Введите API ключ RuCaptcha")
            return

        region = self.region_var.get().strip()
        if not region:
            messagebox.showerror("Ошибка", "Укажите регион")
            return

        digits_count = self.digits_var.get()
        if digits_count < 1 or digits_count > 4:
            messagebox.showerror("Ошибка", "Количество цифр должно быть от 1 до 4")
            return

        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')

        num_threads = self.threads_var.get()

        self.log_message(f"🚀 Запуск {num_threads} потоков для региона {region}", "SUCCESS")
        self.log_message(f"🔢 Поиск по {digits_count} цифрам", "SUCCESS")

        def start_threads():
            for i in range(num_threads):
                if not self.parsers or len(self.parsers) <= i:
                    # Получаем прокси для потока
                    proxy_config = self.get_proxy_config(i)
                    
                    # Передаем настройки задержки
                    min_delay = self.min_delay_var.get()
                    max_delay = self.max_delay_var.get()
                    
                    parser = MegafonCaptchaParser(
                        rucaptcha_api_key=self.api_key_var.get(),
                        proxy_config=proxy_config,
                        region=region,
                        thread_id=i+1,
                        gui_callback=self.log_message,
                        base_folder=self.base_folder,
                        shared_numbers=self.shared_numbers,
                        shared_duplicates=self.shared_duplicates,
                        digits_count=digits_count
                    )
                    
                    # Устанавливаем настройки задержки
                    parser.delay_settings['min_delay'] = min_delay
                    parser.delay_settings['max_delay'] = max_delay
                    
                    self.parsers.append(parser)
                    
                    time.sleep(5)  # Увеличенная задержка между запуском потоков
                    
                    thread = threading.Thread(target=parser.start)
                    thread.daemon = True
                    thread.start()
                    self.threads.append(thread)
                    
                    proxy_info = f" с прокси: {proxy_config['server']}" if proxy_config else " без прокси"
                    self.log_message(f"✅ Поток {i+1} запущен для региона {region}{proxy_info}", "SUCCESS")

        threading.Thread(target=start_threads, daemon=True).start()

    def stop_parsing(self):
        """Останавливает парсеры"""
        for parser in self.parsers:
            parser.stop()
        
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')
        self.log_message("🛑 Все парсеры остановлены", "SUCCESS")

    def save_numbers(self):
        """Сохраняет номера"""
        try:
            filename = filedialog.asksaveasfilename(
                defaultextension=".txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
            )
            if filename:
                with open(filename, 'w', encoding='utf-8') as f:
                    for number in sorted(self.shared_numbers):
                        f.write(f"+7{number}\n")
                
                self.log_message(f"💾 Сохранено {len(self.shared_numbers)} номеров", "SUCCESS")
        except Exception as e:
            self.log_message(f"❌ Ошибка сохранения: {e}", "ERROR")

    def update_stats(self):
        """Обновляет статистику"""
        try:
            stats_text = "=== 📊 СТАТИСТИКА ===\n\n"
            total_numbers = len(self.shared_numbers)
            
            active_parsers = 0
            total_combinations = 0
            total_captchas_solved = 0
            total_delay_multiplier = 0.0
            current_combinations = []
            
            for parser in self.parsers:
                stats = parser.get_stats()
                stats_text += f"📡 Поток {stats['thread_id']} ({stats['region']}):\n"
                stats_text += f"  🟢 Статус: {stats['status']}\n"
                stats_text += f"  📞 Номеров: {stats['numbers_found']}\n"
                stats_text += f"  🔢 Комбинаций: {stats['combinations_tried']}\n"
                stats_text += f"  🧩 Капч решено: {stats['captchas_solved']}\n"
                stats_text += f"  🔄 Дубликатов: {stats['duplicates']}\n"
                stats_text += f"  🔢 Цифр в поиске: {stats['digits_count']}\n"
                stats_text += f"  ⏱️ Множитель задержки: {stats['delay_multiplier']:.1f}x\n"
                if stats['current_combination']:
                    stats_text += f"  🔍 Текущая: {stats['current_combination']}\n"
                stats_text += f"  ⏰ Активность: {stats['last_activity']}\n\n"
                
                if stats['is_running']:
                    active_parsers += 1
                total_combinations += stats['combinations_tried']
                total_captchas_solved += stats['captchas_solved']
                total_delay_multiplier += stats['delay_multiplier']
                if stats['current_combination']:
                    current_combinations.append(stats['current_combination'])
            
            avg_delay_multiplier = total_delay_multiplier / len(self.parsers) if self.parsers else 1.0
            
            stats_text += f"=== 🎯 ИТОГО ===\n"
            stats_text += f"Активных потоков: {active_parsers}/{len(self.parsers)}\n"
            stats_text += f"📊 ОБЩИХ номеров: {total_numbers}\n"
            stats_text += f"🔢 Всего комбинаций: {total_combinations}\n"
            stats_text += f"🧩 Капч решено: {total_captchas_solved}\n"
            stats_text += f"🔄 Общих дубликатов: {sum(self.shared_duplicates.values())}\n"
            stats_text += f"📡 Доступных прокси: {len(self.proxies)}\n"
            stats_text += f"⚠️ Нерабочих прокси: {len(self.bad_proxies)}\n"
            stats_text += f"⏱️ Средний множитель задержки: {avg_delay_multiplier:.1f}x\n"
            if current_combinations:
                stats_text += f"🔍 Текущие комбинации: {', '.join(current_combinations)}\n"
            stats_text += f"⏰ Время: {datetime.now().strftime('%H:%M:%S')}"
            
            self.stats_text.delete(1.0, tk.END)
            self.stats_text.insert(1.0, stats_text)
            
            self.total_numbers_found = total_numbers
            self.counter_var.set(f"📞 Всего номеров: {total_numbers} | 🔢 Комбинаций: {total_combinations} | 🧩 Капч: {total_captchas_solved}")
            
        except Exception as e:
            pass
        
        self.root.after(2000, self.update_stats)

    def run(self):
        """Запускает GUI"""
        self.root.mainloop()


if __name__ == "__main__":
    gui = MegafonParserGUI()
    gui.run()