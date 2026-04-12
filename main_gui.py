"""
main_gui.py

이 스크립트는 스타필드 한국어 번역 툴킷의 전체 파이프라인(Step 1 ~ Step 5)을
시각적으로 손쉽게 실행하고 제어할 수 있도록 도와주는 PyQt6 기반의 GUI(그래픽 유저 인터페이스) 프론트엔드입니다.
LLM API 키 관리, 실행 옵션 설정, 로그 확인 및 작업 중단 기능을 제공합니다.
"""
import sys
import os
import json
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit,
    QFileDialog, QMessageBox, QGroupBox, QFormLayout, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QSplitter
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

CONFIG_FILE = "config.json"

import base64

def _obfuscate(text: str) -> str:
    if not text: return text
    key = "STARFIELD"
    obfuscated = "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(text))
    return base64.b64encode(obfuscated.encode('utf-8')).decode('utf-8')

def _deobfuscate(text: str) -> str:
    if not text: return text
    try:
        decoded = base64.b64decode(text.encode('utf-8')).decode('utf-8')
        key = "STARFIELD"
        deobfuscated = "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
        return deobfuscated
    except:
        return text

class ConfigManager:
    """
    config.json 파일과 연동되어 사용자의 API 키, 환경 설정, 시스템 프롬프트 등을
    불러오고(Load) 저장하는(Save) 역할을 담당하는 관리 클래스입니다.
    """
    def __init__(self):
        self.config = {
            "api_provider": "gemini",
            "gemini_api_key": "",
            "openai_api_key": "",
            "1minai_api_key": "",
            "localllm_base_url": "http://localhost:11434/v1",
            "localllm_api_key": "",
            "gcp_project_id": "project-2c984893-491f-4636-adf",
            "gcp_location": "asia-northeast1",
            "model_name": "gemini-2.5-flash",
            "glossary_file": str(Path(__file__).parent / "glossary.json"),
            "rag_data_file": str(Path(__file__).parent / "train_data_final.json"),
            "step4_prompt": "당신은 베데스다 스타필드 게임의 UI, 아이템, 일지 등을 번역하는 전문가입니다. 원문의 의도를 파악하여 직관적이고 게임에 어울리는 한국어로 번역하세요. 제공된 용어집을 무조건 준수하며, 결과는 오직 JSON 배열로만 반환하십시오. 절대 다른 말을 덧붙이지 마십시오.",
            "step5_prompt": "당신은 베데스다 스타필드 게임의 UI, 아이템, 일지 등을 번역하는 전문가입니다. 원문의 의도를 파악하여 직관적이고 게임에 어울리는 한국어로 번역하세요. 제공된 용어집을 무조건 준수하며, 결과는 오직 JSON 배열로만 반환하십시오. 절대 다른 말을 덧붙이지 마십시오.",
            "use_ja_ref": False,
            "auto_audio_analysis": False,
            "pipeline_mode": "high_quality",
            "orchestrator": {
                "enabled": False,
                "generation_models": [
                    {"provider": "1minai", "model": "gpt-4o-mini", "persona": "Natural: 가장 자연스럽고 구어체적인 한국어 대사체에 집중하세요."},
                    {"provider": "1minai", "model": "claude-3-5-sonnet", "persona": "Faithful: 원문의 의미와 문장 구조를 최대한 유지하며 오역 없는 번역에 집중하세요."}
                ],
                "review_model": {"provider": "vertexai", "model": "gemini-2.5-pro"}
            }
        }
        self.load()

    def load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    for k in ["gemini_api_key", "openai_api_key", "localllm_api_key", "1minai_api_key"]:
                        if k in loaded and loaded[k]:
                            loaded[k] = _deobfuscate(loaded[k])
                    self.config.update(loaded)
            except Exception as e:
                print(f"Failed to load config: {e}")

    def save(self):
        try:
            to_save = dict(self.config)
            for k in ["gemini_api_key", "openai_api_key", "localllm_api_key", "1minai_api_key"]:
                if k in to_save and to_save[k]:
                    to_save[k] = _obfuscate(to_save[k])
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Failed to save config: {e}")


class WorkerThread(QThread):
    """
    메인 GUI 프로그램이 멈추지(Freeze) 않도록, 백그라운드 스레드에서
    실제 번역 스크립트(step1 ~ step5)를 서브프로세스로 실행하고
    출력되는 로그를 실시간으로 GUI 메인 스레드에 전달하는 역할을 합니다.
    """
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str, str)

    def __init__(self, step, kwargs):
        super().__init__()
        self.step = step
        self.kwargs = kwargs
        self._is_stopped = False
        self.process = None

    def stop(self):
        self._is_stopped = True
        if self.process:
            self.process.terminate()
            self.log_signal.emit(f"[{self.step}] 작업 중지 요청됨...")

    def run(self):
        self.log_signal.emit(f"[{self.step}] 작업을 시작합니다...")
        self.log_signal.emit(f"Arguments: {self.kwargs}")
        
        try:
            import subprocess
            import sys
            
            script_map = {
                "Step1": "step1_extract_scene.py",
                "Step2": "step2_translate_scene.py",
                "Step3": "step3_build_xml.py",
                "Step4": "step4_translate_xml.py",
                "Step5": "step5_review_xml.py",
                "Step6": "step6_mod_update.py",
                "AudioExtract": "extract_audio.py",
                "AudioProfile": "audition_profiler.py",
                "AutoPipeline": "auto_pipeline.py"
            }
            script_file = script_map.get(self.step)
            if not script_file:
                self.finished_signal.emit(False, "알 수 없는 배치 작업입니다.", self.step)
                return

            cmd = [sys.executable, script_file]
            
            if self.step == "Step1":
                cmd.extend(["-i", self.kwargs["input"], "-o", self.kwargs["output"]])
                if self.kwargs.get("strings"):
                    cmd.extend(["-s", self.kwargs["strings"]])
                if self.kwargs.get("use_ja_ref"):
                    cmd.append("--use-ja-ref")
            elif self.step == "Step2":
                cmd.extend(["-i", self.kwargs["input"], "-o", self.kwargs["output"]])
                if self.kwargs.get("profile_only"):
                    cmd.append("--profile-only")
                if self.kwargs.get("use_ja_ref"):
                    cmd.append("--use_ja_ref")
            elif self.step == "AutoPipeline":
                cmd.extend(["-i", self.kwargs["input"]])
                if self.kwargs.get("config"):
                    cmd.extend(["-c", self.kwargs["config"]])
            elif self.step == "Step3":
                if self.kwargs.get("merge_xml"):
                    # XML 머지 모드: --merge-xml <기존xml> -t <json> [-o <출력xml>]
                    cmd.extend(["-x", self.kwargs["merge_xml"]])
                    cmd.extend(["-t", self.kwargs["trans"]])
                    if self.kwargs.get("out"):
                        cmd.extend(["-o", self.kwargs["out"]])
                else:
                    # ESM 모드 (기존 동작)
                    cmd.extend(["-i", self.kwargs["esm"]])
                    if self.kwargs.get("trans"):
                        cmd.extend(["-t", self.kwargs["trans"]])
                    if self.kwargs.get("out"):
                        cmd.extend(["-o", self.kwargs["out"]])
            elif self.step == "Step4":
                cmd.extend(["-i", self.kwargs["input"], "-o", self.kwargs["output"]])
                if self.kwargs.get("use_ja_ref"):
                    cmd.append("--use-ja-ref")
            elif self.step == "Step5":
                cmd.extend(["-i", self.kwargs["input"], "-o", self.kwargs["output"]])
                if self.kwargs.get("scan_only"):
                    cmd.append("--scan-only")
                elif self.kwargs.get("indices"):
                    cmd.extend(["--translate-indices", self.kwargs["indices"]])
            elif self.step == "Step6":
                cmd.extend(["-i", self.kwargs["input"], "-m", self.kwargs["mode"], "-o", self.kwargs["output"]])
                if self.kwargs.get("profile"):
                    cmd.extend(["-p", self.kwargs["profile"]])
                if self.kwargs.get("reference"):
                    cmd.extend(["-r", self.kwargs["reference"]])
            elif self.step == "AudioExtract":
                cmd.extend(["-p", self.kwargs["priority_list"]])
                cmd.extend(["-d", self.kwargs["data_dir"]])
                cmd.extend(["-o", self.kwargs["output_dir"]])
            elif self.step == "AudioProfile":
                cmd.extend(["-c", self.kwargs["config"]])
                cmd.extend(["-p", self.kwargs["priority_list"]])
                cmd.extend(["-a", self.kwargs["audition_dir"]])
                cmd.extend(["-o", self.kwargs["output"]])
            
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000) if os.name == 'nt' else 0
            )

            for line in iter(self.process.stdout.readline, ""):
                if self._is_stopped:
                    break
                if line:
                    self.log_signal.emit(line.strip("\n"))
            
            self.process.stdout.close()
            
            if self._is_stopped:
                self.process.terminate()
                self.process.wait()
                self.log_signal.emit(f"[{self.step}] 작업이 중지되었습니다.")
                self.finished_signal.emit(False, "사용자에 의해 강제 종료됨.", self.step)
                return

            return_code = self.process.wait()
            
            if return_code == 0:
                self.log_signal.emit(f"[{self.step}] 작업 완료!")
                self.finished_signal.emit(True, "성공적으로 끝났습니다.", self.step)
            else:
                self.finished_signal.emit(False, f"프로세스 비정상 종료 (코드: {return_code})", self.step)
        except Exception as e:
            self.finished_signal.emit(False, str(e), self.step)
        finally:
            self.process = None


class MainApp(QMainWindow):
    """
    PyQt6 메인 애플리케이션 창(Window)을 구성하는 클래스입니다.
    각 작업 단계별로 탭(Tab)이 나누어져 있으며, 설정 관리와 상태 로깅 인터페이스를 구축합니다.
    """
    def __init__(self):
        super().__init__()
        self.config_mgr = ConfigManager()
        self.worker = None
        self.init_ui()

    def init_ui(self):
        self.base_title = "Starfield Translation Tool v2.0"
        self.setWindowTitle(self.base_title)
        self.resize(1200, 800)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        self.tabs.addTab(self.create_auto_pipeline_tab(), "🚀 원클릭 자동 번역")
        self.tabs.addTab(self.create_settings_tab(), "공통 설정")
        self.tabs.addTab(self.create_db_manager_tab(), "DB 관리 (용어+TM)")
        self.tabs.addTab(self.create_step1_tab(), "Step 1: Scene 추출")
        self.tabs.addTab(self.create_step2_tab(), "Step 2: Scene 번역")
        self.tabs.addTab(self.create_step3_tab(), "Step 3: XML 빌드")
        self.tabs.addTab(self.create_step4_tab(), "Step 4: XML 번역")
        self.tabs.addTab(self.create_step5_tab(), "Step 5: 미번역 보완+태그 검사")
        self.tabs.addTab(self.create_step6_tab(), "Step 6: 모드/DLC 번역 교정")
        self.tabs.addTab(self.create_audio_tab(), "멀티모달 오디오 오디션")

        self.status_log = QTextEdit()
        self.status_log.setReadOnly(True)
        self.status_log.setMinimumHeight(250)
        self.main_layout.addWidget(QLabel("통합 로그:"), 0)
        self.main_layout.addWidget(self.status_log, 1)

    def append_log(self, text):
        self.status_log.append(text)

    # ==========================
    # 0. 자동화 파이프라인 탭
    # ==========================
    def create_auto_pipeline_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        banner = QLabel("🌌 Starfield Mod 원클릭 자동 번역 시스템")
        banner.setStyleSheet("font-size: 18px; font-weight: bold; color: #1e88e5; margin: 10px;")
        layout.addWidget(banner)

        desc = QLabel("번역할 모드 파일(ESM/ESP)을 선택하면, 대화 유무를 자동으로 판단하여\n추출부터 최종 XML 생성까지 모든 단계를 한 번에 처리합니다.")
        desc.setStyleSheet("color: #555; margin-bottom: 20px;")
        layout.addWidget(desc)

        group = QGroupBox("자동화 설정")
        form = QFormLayout(group)

        self.auto_input_path = QLineEdit()
        btn_browse = QPushButton("모드 파일 찾기")
        btn_browse.clicked.connect(self.browse_auto_input)
        row1 = QHBoxLayout()
        row1.addWidget(self.auto_input_path)
        row1.addWidget(btn_browse)
        form.addRow("대상 기가바이트 파일 (.esm / .esp):", row1)

        self.auto_mode_cb = QComboBox()
        self.auto_mode_cb.addItems(["high_quality (다중 모델 후보 생성/감수)", "fast (단일 모델 고속 번역)"])
        self.auto_mode_cb.setCurrentIndex(0 if self.config_mgr.config.get("pipeline_mode") == "high_quality" else 1)
        form.addRow("번역 품질 설정:", self.auto_mode_cb)

        self.auto_audio_check = QCheckBox("대화 발견 시 자동으로 음성 분석 및 프로파일 생성")
        self.auto_audio_check.setChecked(self.config_mgr.config.get("auto_audio_analysis", False))
        form.addRow("오디오 연동:", self.auto_audio_check)

        layout.addWidget(group)

        self.auto_start_btn = QPushButton("🚀 자동 번역 파이프라인 시작")
        self.auto_start_btn.setMinimumHeight(50)
        self.auto_start_btn.setStyleSheet("background-color: #1976d2; color: white; font-size: 16px; font-weight: bold;")
        self.auto_start_btn.clicked.connect(self.run_auto_pipeline)
        
        self.auto_stop_btn = QPushButton("정지")
        self.auto_stop_btn.setEnabled(False)
        self.auto_stop_btn.clicked.connect(self.stop_current_task)

        h_btn = QHBoxLayout()
        h_btn.addWidget(self.auto_start_btn)
        h_btn.addWidget(self.auto_stop_btn)
        layout.addLayout(h_btn)

        layout.addStretch()
        return widget

    def browse_auto_input(self):
        path, _ = QFileDialog.getOpenFileName(self, "모드 파일 선택", "", "Bethesda Plugin Files (*.esm *.esp);;All Files (*)")
        if path:
            self.auto_input_path.setText(path)

    def run_auto_pipeline(self):
        if not self.auto_input_path.text():
            QMessageBox.warning(self, "경고", "번역할 모드 파일을 먼저 선택하세요.")
            return

        # 설정 업데이트
        self.config_mgr.config["auto_audio_analysis"] = self.auto_audio_check.isChecked()
        self.config_mgr.config["pipeline_mode"] = "high_quality" if self.auto_mode_cb.currentIndex() == 0 else "fast"
        self.config_mgr.save()

        self.run_background_task("AutoPipeline", {
            "input": self.auto_input_path.text(),
            "config": "config.json"
        }, self.auto_stop_btn)

    # ==========================
    # 1. 공통 설정 탭
    # ==========================
    def create_settings_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        form = QFormLayout()
        self.settings_form = form

        self.api_provider_combo = QComboBox()
        self.api_provider_combo.addItems(["vertexai", "gemini", "openai", "localllm", "1minai"])
        if self.config_mgr.config.get("api_provider", "gemini") in ["vertexai", "gemini", "openai", "localllm", "1minai"]:
            self.api_provider_combo.setCurrentText(self.config_mgr.config.get("api_provider", "gemini"))
        self.api_provider_combo.currentTextChanged.connect(self.on_provider_changed)

        self.gemini_key_input = QLineEdit(self.config_mgr.config.get("gemini_api_key", ""))
        self.gemini_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        
        self.openai_key_input = QLineEdit(self.config_mgr.config.get("openai_api_key", ""))
        self.openai_key_input.setEchoMode(QLineEdit.EchoMode.Password)

        self.min1ai_key_input = QLineEdit(self.config_mgr.config.get("1minai_api_key", ""))
        self.min1ai_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        
        self.gcp_id_input = QLineEdit(self.config_mgr.config.get("gcp_project_id", ""))
        self.gcp_loc_input = QLineEdit(self.config_mgr.config.get("gcp_location", ""))
        
        self.gcp_key_path_input = QLineEdit(self.config_mgr.config.get("gcp_key_json", ""))
        self.gcp_key_btn = QPushButton("찾아보기")
        self.gcp_key_btn.clicked.connect(lambda: self.browse_file(self.gcp_key_path_input, "JSON Files (*.json);;All Files (*)"))
        self.row_gcp_key = QHBoxLayout()
        self.row_gcp_key.addWidget(self.gcp_key_path_input)
        self.row_gcp_key.addWidget(self.gcp_key_btn)
        
        self.localllm_base_url_input = QLineEdit(self.config_mgr.config.get("localllm_base_url", "http://localhost:11434/v1"))
        self.localllm_api_key_input = QLineEdit(self.config_mgr.config.get("localllm_api_key", ""))
        self.localllm_api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        form.addRow("API 지원 모드 (Provider):", self.api_provider_combo)
        form.addRow("Gemini API Key:", self.gemini_key_input)
        form.addRow("OpenAI API Key:", self.openai_key_input)
        form.addRow("1min.ai API Key:", self.min1ai_key_input)
        form.addRow("GCP Project ID (Vertex AI 전용):", self.gcp_id_input)
        form.addRow("GCP Location (Vertex AI 전용):", self.gcp_loc_input)
        form.addRow("GCP Key JSON Path:", self.row_gcp_key)
        form.addRow("Local LLM Base URL:", self.localllm_base_url_input)
        form.addRow("Local LLM API Key:", self.localllm_api_key_input)
        form.addRow("Model Name:", self.model_combo)
        
        self.on_provider_changed(self.api_provider_combo.currentText())
        
        current_model = self.config_mgr.config.get("model_name", "gemini-2.5-flash")
        if self.model_combo.findText(current_model) >= 0:
            self.model_combo.setCurrentText(current_model)
        else:
            self.model_combo.setCurrentText(current_model)
        
        layout.addLayout(form)

        orch_group = QGroupBox("멀티 모델 오케스트레이터 (Orchestrator) 설정")
        orch_layout = QVBoxLayout()
        
        self.orch_enabled_check = QCheckBox("오케스트레이터 활성화 (다중 모델 후보 생성 + 수석 에디터 감수)")
        orch_data = self.config_mgr.config.get("orchestrator", {})
        self.orch_enabled_check.setChecked(orch_data.get("enabled", False))
        orch_layout.addWidget(self.orch_enabled_check)
        
        self.orch_config_edit = QTextEdit()
        self.orch_config_edit.setPlaceholderText("오케스트레이터 세부 설정 (JSON)")
        self.orch_config_edit.setPlainText(json.dumps({
            "generation_models": orch_data.get("generation_models", []),
            "review_model": orch_data.get("review_model", {})
        }, ensure_ascii=False, indent=2))
        self.orch_config_edit.setMaximumHeight(150)
        orch_layout.addWidget(QLabel("세부 모델 설정 (JSON 구조):"))
        orch_layout.addWidget(self.orch_config_edit)
        
        orch_group.setLayout(orch_layout)
        layout.addWidget(orch_group)

        save_btn = QPushButton("설정 저장")
        save_btn.clicked.connect(self.save_settings)
        layout.addWidget(save_btn)
        
        self.use_ja_ref_check = QCheckBox("일본어 원문 참조 모드 활성화 (공유 설정)")
        self.use_ja_ref_check.setChecked(self.config_mgr.config.get("use_ja_ref", False))
        layout.addWidget(self.use_ja_ref_check)
        
        layout.addStretch()
        return widget

    def browse_file(self, line_edit, filter_str):
        path, _ = QFileDialog.getOpenFileName(self, "파일 선택", "", filter_str)
        if path:
            line_edit.setText(path)

    def browse_save_file(self, line_edit, filter_str):
        path, _ = QFileDialog.getSaveFileName(self, "파일 저장", "", filter_str)
        if path:
            line_edit.setText(path)

    def browse_folder(self, line_edit):
        path = QFileDialog.getExistingDirectory(self, "폴더 선택", "")
        if path:
            line_edit.setText(path)

    def on_provider_changed(self, provider):
        self.model_combo.clear()
        
        controls = {
            "vertexai": [self.gcp_id_input, self.gcp_loc_input, self.gcp_key_path_input, self.gcp_key_btn],
            "gemini": [self.gemini_key_input],
            "openai": [self.openai_key_input],
            "1minai": [self.min1ai_key_input],
            "localllm": [self.localllm_base_url_input, self.localllm_api_key_input]
        }
        
        if hasattr(self, 'settings_form'):
            for prov, widgets in controls.items():
                is_visible = (prov == provider)
                for w in widgets:
                    w.setVisible(is_visible)
                    label = self.settings_form.labelForField(w)
                    if not label: 
                        if w == self.gcp_key_path_input:
                             label = self.settings_form.labelForField(self.row_gcp_key)
                    if label:
                        label.setVisible(is_visible)

        if provider == "openai":
            self.model_combo.addItems(["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-mini", "o3-mini"])
        elif provider == "1minai":
            self.model_combo.addItems(["gpt-4o-mini", "claude-3-5-sonnet", "gemini-1.5-pro", "gpt-4o"])
        elif provider == "localllm":
            self.model_combo.addItems(["hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4", "gemma-2", "qwen-2.5", "deepseek-r1"])
        elif provider == "vertexai":
            self.model_combo.addItems(["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro-002", "gemini-1.5-flash-002"])
        else:
            self.model_combo.addItems(["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro-002", "gemini-1.5-flash-002"])

    def save_settings(self):
        self.config_mgr.config["api_provider"] = self.api_provider_combo.currentText()
        self.config_mgr.config["gemini_api_key"] = self.gemini_key_input.text()
        self.config_mgr.config["openai_api_key"] = self.openai_key_input.text()
        self.config_mgr.config["1minai_api_key"] = self.min1ai_key_input.text()
        self.config_mgr.config["localllm_base_url"] = self.localllm_base_url_input.text()
        self.config_mgr.config["localllm_api_key"] = self.localllm_api_key_input.text()
        self.config_mgr.config["gcp_project_id"] = self.gcp_id_input.text()
        self.config_mgr.config["gcp_location"] = self.gcp_loc_input.text()
        self.config_mgr.config["gcp_key_json"] = self.gcp_key_path_input.text()
        self.config_mgr.config["model_name"] = self.model_combo.currentText()
        
        try:
            orch_json = json.loads(self.orch_config_edit.toPlainText())
            self.config_mgr.config["orchestrator"] = {
                "enabled": self.orch_enabled_check.isChecked(),
                "generation_models": orch_json.get("generation_models", []),
                "review_model": orch_json.get("review_model", {})
            }
        except Exception as e:
            self.append_log(f"오케스트레이터 JSON 파싱 오류: {e}")

        if hasattr(self, 'step4_prompt_edit'):
            self.config_mgr.config["step4_prompt"] = self.step4_prompt_edit.toPlainText()
        if hasattr(self, 'step5_prompt_edit'):
            self.config_mgr.config["step5_prompt"] = self.step5_prompt_edit.toPlainText()
        self.config_mgr.config["use_ja_ref"] = self.use_ja_ref_check.isChecked()
            
        self.config_mgr.save()
        self.append_log("모든 설정 및 프롬프트가 저장되었습니다.")
        QMessageBox.information(self, "알림", "모든 설정 및 프롬프트가 저장되었습니다.")

    def create_db_manager_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        top_layout = QHBoxLayout()
        self.db_table_combo = QComboBox()
        self.db_table_combo.addItems(["glossary", "translation_memory", "npc_names"])
        top_layout.addWidget(QLabel("선택된 테이블:"))
        top_layout.addWidget(self.db_table_combo)
        
        self.db_search_input = QLineEdit()
        self.db_search_input.setPlaceholderText("검색어 입력 (English/Korean) 후 엔터")
        self.db_search_input.returnPressed.connect(self.load_db_data)
        top_layout.addWidget(self.db_search_input)
        
        search_btn = QPushButton("검색 / 새로고침")
        top_layout.addWidget(search_btn)
        
        layout.addLayout(top_layout)
        
        self.db_table = QTableWidget()
        self.db_table.setColumnCount(3)
        self.db_table.setHorizontalHeaderLabels(["ID (읽기전용)", "영어 (English)", "한국어 (Korean)"])
        self.db_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.db_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.db_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.db_table)
        
        bot_layout = QHBoxLayout()
        add_btn = QPushButton("새 항목 추가 (최하단)")
        del_btn = QPushButton("선택 행 삭제")
        save_btn = QPushButton("수정 사항 DB에 저장")
        bot_layout.addWidget(add_btn)
        bot_layout.addWidget(del_btn)
        bot_layout.addWidget(save_btn)
        
        layout.addLayout(bot_layout)
        
        self.db_table_combo.currentTextChanged.connect(self.load_db_data)
        search_btn.clicked.connect(self.load_db_data)
        add_btn.clicked.connect(self.add_db_row)
        del_btn.clicked.connect(self.delete_db_row)
        save_btn.clicked.connect(self.save_db_changes)
        
        self.load_db_data()
        return widget
        
    def get_db_connection(self):
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).parent.resolve() / "translation_db.sqlite"
        return sqlite3.connect(db_path)

    def load_db_data(self):
        table = self.db_table_combo.currentText()
        if table not in ("glossary", "translation_memory", "npc_names"):
            return
            
        search_query = self.db_search_input.text().strip()
        
        col_map = {
            "glossary": ("english", "korean", ["ID (읽기전용)", "영어 (English)", "한국어 (Korean)"]),
            "translation_memory": ("english", "korean", ["ID (읽기전용)", "영어 (English)", "한국어 (Korean)"]),
            "npc_names": ("form_id", "name", ["ID (읽기전용)", "FormID (8자리 Hex)", "이름 (Name)"])
        }
        c1, c2, headers = col_map[table]
        self.db_table.setHorizontalHeaderLabels(headers)
        
        try:
            conn = self.get_db_connection()
            c = conn.cursor()
            query = f"SELECT id, {c1}, {c2} FROM {table}"
            params = ()
            if search_query:
                query += f" WHERE {c1} LIKE ? OR {c2} LIKE ?"
                like_term = f"%{search_query}%"
                params = (like_term, like_term)
            
            query += " ORDER BY id DESC LIMIT 1000"
            c.execute(query, params)
            rows = c.fetchall()
            conn.close()
            
            self.db_table.setRowCount(0)
            for row_idx, row_data in enumerate((rows)):
                self.db_table.insertRow(row_idx)
                for col_idx, col_data in enumerate(row_data):
                    item = QTableWidgetItem(str(col_data))
                    if col_idx == 0:
                        item.setFlags(item.flags() ^ Qt.ItemFlag.ItemIsEditable)
                    self.db_table.setItem(row_idx, col_idx, item)
        except Exception as e:
            QMessageBox.warning(self, "DB Error", f"데이터 로드 실패 (DB가 없을 경우 마이그레이션을 우선 실행하세요): {e}")

    def add_db_row(self):
        row_count = self.db_table.rowCount()
        self.db_table.insertRow(row_count)
        id_item = QTableWidgetItem("NEW")
        id_item.setFlags(id_item.flags() ^ Qt.ItemFlag.ItemIsEditable)
        self.db_table.setItem(row_count, 0, id_item)
        self.db_table.setItem(row_count, 1, QTableWidgetItem(""))
        self.db_table.setItem(row_count, 2, QTableWidgetItem(""))
        self.db_table.scrollToBottom()

    def delete_db_row(self):
        selected_rows = set(index.row() for index in self.db_table.selectedIndexes())
        if not selected_rows:
            QMessageBox.information(self, "알림", "삭제할 행을 클릭해서 선택하세요.")
            return
            
        table = self.db_table_combo.currentText()
        conn = self.get_db_connection()
        c = conn.cursor()
        
        # 큰 인덱스부터 지워야 UI 꼬임 방지
        for row in sorted(selected_rows, reverse=True):
            id_item = self.db_table.item(row, 0)
            if id_item and id_item.text() != "NEW":
                try:
                    c.execute(f"DELETE FROM {table} WHERE id=?", (id_item.text(),))
                except Exception as e:
                    self.append_log(f"DB 삭제 오류(ID: {id_item.text()}): {e}")
            self.db_table.removeRow(row)
            
        conn.commit()
        conn.close()
        self.append_log(f"[{table}] 선택 항목이 삭제되었습니다.")

    def save_db_changes(self):
        try:
            table = self.db_table_combo.currentText()
            conn = self.get_db_connection()
            c = conn.cursor()
            
            for row in range(self.db_table.rowCount()):
                id_item = self.db_table.item(row, 0)
                en_item = self.db_table.item(row, 1)
                ko_item = self.db_table.item(row, 2)
                
                if not id_item or not en_item or not ko_item: continue
                    
                en_text = en_item.text().strip()
                ko_text = ko_item.text().strip()
                
                if not en_text: continue
                
                if id_item.text() == "NEW":
                    if table == "translation_memory":
                        c.execute(f"INSERT OR IGNORE INTO {table} (english, korean, english_length) VALUES (?, ?, ?)", (en_text, ko_text, len(en_text)))
                    else:
                        c.execute(f"INSERT OR IGNORE INTO {table} (english, korean) VALUES (?, ?)", (en_text, ko_text))
                else:
                    if table == "translation_memory":
                        c.execute(f"UPDATE {table} SET english=?, korean=?, english_length=? WHERE id=?", (en_text, ko_text, len(en_text), id_item.text()))
                    else:
                        c.execute(f"UPDATE {table} SET english=?, korean=? WHERE id=?", (en_text, ko_text, id_item.text()))
                    
            conn.commit()
            conn.close()
            self.load_db_data() # 새로고침
            self.append_log(f"[{table}] 수정 사항이 성공적으로 저장되었습니다.")
            QMessageBox.information(self, "완료", "저장되었습니다.")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"저장 중 오류 발생: {e}")

    # ==========================
    # 2. Step 1 추출 탭
    # ==========================
    def create_step1_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        form = QFormLayout()
        
        self.step1_input = QLineEdit()
        btn = QPushButton("찾아보기")
        btn.clicked.connect(self.browse_step1_esm)
        row1 = QHBoxLayout()
        row1.addWidget(self.step1_input)
        row1.addWidget(btn)
        form.addRow("입력 파일 (ESM/ESP):", row1)
        
        self.step1_output = QLineEdit()
        self.step1_output.setPlaceholderText("ESM 폴더에 _dump.json으로 자동 저장")
        form.addRow("출력 파일명:", self.step1_output)
        
        self.step1_strings = QLineEdit()
        self.step1_strings.setPlaceholderText("비워두면 ESM 폴더 또는 하위 Strings 폴더에서 자동 탐색")
        btn_s = QPushButton("찾아보기")
        btn_s.clicked.connect(lambda: self.browse_folder(self.step1_strings))
        row_s = QHBoxLayout()
        row_s.addWidget(self.step1_strings)
        row_s.addWidget(btn_s)
        form.addRow("Strings 폴더 (선택):", row_s)
        
        layout.addLayout(form)
        
        btn_layout = QHBoxLayout()
        run_btn = QPushButton("Step 1 씬 추출 실행")
        run_btn.clicked.connect(self.run_step1)
        self.step1_stop_btn = QPushButton("정지")
        self.step1_stop_btn.clicked.connect(self.stop_current_task)
        
        self.step1_ja_check = QCheckBox("일본어 원문 동시 추출 (JA Strings 필요)")
        self.step1_ja_check.setChecked(self.config_mgr.config.get("use_ja_ref", False))
        layout.addWidget(self.step1_ja_check)
        
        btn_layout.addWidget(run_btn)
        btn_layout.addWidget(self.step1_stop_btn)
        layout.addLayout(btn_layout)
        
        layout.addStretch()
        return widget

    def browse_step1_esm(self):
        path, _ = QFileDialog.getOpenFileName(self, "Bethesda Plugin 파일 선택", "", "Bethesda Plugin Files (*.esm *.esp);;All Files (*)")
        if path:
            self.step1_input.setText(path)
            # 입력 폴더 기준으로 출력 경로 세팅
            dir_name = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.step1_output.setText(os.path.join(dir_name, f"{base_name}_dump.json"))

    def run_step1(self):
        self.run_background_task("Step1", {
            "input": self.step1_input.text(), 
            "output": self.step1_output.text(),
            "strings": self.step1_strings.text().strip(),
            "use_ja_ref": self.step1_ja_check.isChecked()
        }, self.step1_stop_btn)

    # ==========================
    # 3. Step 2 번역 탭
    # ==========================
    def create_step2_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        form = QFormLayout()
        self.step2_input = QLineEdit()
        self.step2_input.setPlaceholderText("xxx_dump.json")
        btn2 = QPushButton("찾아보기")
        btn2.clicked.connect(self.browse_step2_json)
        row2 = QHBoxLayout()
        row2.addWidget(self.step2_input)
        row2.addWidget(btn2)
        form.addRow("입력 JSON (Step 1 결과):", row2)
        
        self.step2_output = QLineEdit()
        form.addRow("출력 프롬프트 결과 JSON:", self.step2_output)
        layout.addLayout(form)
        
        # 텍스트가 직접 입력/변경될 때도 프로파일 파일을 자동 탐색하도록 이벤트 연결
        self.step2_output.textChanged.connect(self.load_step2_profile_file)
        
        # --- 어조 가이드 기능 추가 ---
        profile_group = QGroupBox("어조 가이드 (사전 추출/편집)")
        profile_layout = QVBoxLayout()
        
        p_top_layout = QHBoxLayout()
        self.step2_profile_btn = QPushButton("어조 가이드 우선 추출")
        self.step2_profile_btn.clicked.connect(self.run_step2_profile)
        p_top_layout.addWidget(self.step2_profile_btn)
        
        self.step2_profile_save_btn = QPushButton("가이드 저장")
        self.step2_profile_save_btn.clicked.connect(self.save_step2_profile_file)
        self.step2_profile_save_btn.setEnabled(False)
        p_top_layout.addWidget(self.step2_profile_save_btn)
        
        profile_layout.addLayout(p_top_layout)
        
        self.step2_profile_edit = QTextEdit()
        self.step2_profile_edit.setPlaceholderText("우선 추출 버튼을 누르면 이 곳에 생성된 가이드 JSON이 표시됩니다.\n직접 수정하고 [가이드 저장] 후 번역을 실행하면 본 번역에 반영됩니다.")
        profile_layout.addWidget(self.step2_profile_edit)
        
        profile_group.setLayout(profile_layout)
        layout.addWidget(profile_group)
        # ------------------------
        
        btn_layout = QHBoxLayout()
        run_btn = QPushButton("Step 2 번역 실행")
        run_btn.clicked.connect(self.run_step2)
        self.step2_stop_btn = QPushButton("정지")
        self.step2_stop_btn.setEnabled(False)
        self.step2_stop_btn.clicked.connect(self.stop_current_task)
        
        btn_layout.addWidget(run_btn)
        btn_layout.addWidget(self.step2_stop_btn)
        layout.addLayout(btn_layout)

        self.step2_ja_check = QCheckBox("일본어 원문 참조하여 번역 (뉘앙스 향상)")
        self.step2_ja_check.setChecked(self.config_mgr.config.get("use_ja_ref", False))
        layout.addWidget(self.step2_ja_check)
        
        layout.addStretch()
        return widget
        
    def browse_step2_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "JSON 파일 선택", "", "JSON Files (*.json);;All Files (*)")
        if path:
            self.step2_input.setText(path)
            dir_name = os.path.dirname(path)
            base_name = os.path.basename(path).replace("_dump.json", "").replace(".json", "")
            self.step2_output.setText(os.path.join(dir_name, f"{base_name}_translated_dict.json"))
            self.load_step2_profile_file()
            
    def run_step2_profile(self):
        if not self.step2_input.text():
            QMessageBox.warning(self, "경고", "먼저 입력 JSON 파일을 지정하세요.")
            return
            
        # self.config_mgr.config["step2_prompt"] = self.step2_prompt_edit.toPlainText() (Hardcoded in source)
        # self.config_mgr.save()
        self.run_background_task("Step2", {
            "input": self.step2_input.text(), 
            "output": self.step2_output.text(),
            "profile_only": True
        }, self.step2_stop_btn)

    def load_step2_profile_file(self):
        if not self.step2_input.text(): return
        
        # 후보 파일 목록 (오디오 기반 tone_profiles.json 우선 확인)
        dir_name = os.path.dirname(self.step2_input.text())
        candidates = [
            os.path.join(dir_name, "tone_profiles.json"), # 오디오 분석 결과물
            self.step2_output.text().replace(".json", "_tone_profile.json"),
            os.path.join(dir_name, "tone_profile.json")
        ]
        
        for profile_file in candidates:
            if os.path.exists(profile_file):
                try:
                    # 파일 내용을 불러오기 전, 현재 에디터 내용과 다를 때만 업데이트 (루프 방지)
                    with open(profile_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    if self.step2_profile_edit.toPlainText() != content:
                        self.step2_profile_edit.setPlainText(content)
                        self.step2_profile_save_btn.setEnabled(True)
                        self.append_log(f"톤 가이드 파일({os.path.basename(profile_file)})을 가이드 뷰어에 로드했습니다.")
                    return # 파일을 찾았으므로 루프 종료
                except Exception as e:
                    self.append_log(f"프로파일 로드 실패: {e}")
        
        # 파일을 하나도 못 찾은 경우
        self.step2_profile_edit.clear()
        self.step2_profile_save_btn.setEnabled(False)

    def save_step2_profile_file(self):
        if not self.step2_output.text(): return
        profile_file = self.step2_output.text().replace(".json", "_profile.json")
        try:
            with open(profile_file, "w", encoding="utf-8") as f:
                f.write(self.step2_profile_edit.toPlainText())
            self.append_log("어조 가이드가 성공적으로 수동 저장되었습니다.")
            QMessageBox.information(self, "저장 완료", f"{os.path.basename(profile_file)} 저장 완료")
        except Exception as e:
            QMessageBox.critical(self, "오류", f"저장 실패: {e}")
            
    def run_step2(self):
        # self.config_mgr.config["step2_prompt"] = self.step2_prompt_edit.toPlainText() (Hardcoded in source)
        # self.config_mgr.save()
        self.run_background_task("Step2", {
            "input": self.step2_input.text(), 
            "output": self.step2_output.text(),
            "use_ja_ref": self.step2_ja_check.isChecked()
        }, self.step2_stop_btn)

    # ==========================
    # 4. Step 3 XML 빌드 탭
    # ==========================
    def create_step3_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # ── ESM 모드 그룹 ──────────────────────────────────────────────
        esm_group = QGroupBox("[ESM 모드] ESM → XML 생성 (+ JSON 번역 머지 선택사항)")
        esm_form = QFormLayout()

        self.step3_esm_input = QLineEdit()
        btn3_esm = QPushButton("찾아보기")
        btn3_esm.clicked.connect(self.browse_step3_esm)
        row3_esm = QHBoxLayout()
        row3_esm.addWidget(self.step3_esm_input)
        row3_esm.addWidget(btn3_esm)
        esm_form.addRow("원본 ESM 파일 [-i]:", row3_esm)

        self.step3_trans_input = QLineEdit()
        self.step3_trans_input.setPlaceholderText("xxx_translated_dict.json (선택)")
        btn3_trans = QPushButton("찾아보기")
        btn3_trans.clicked.connect(lambda: self.browse_file(self.step3_trans_input, "JSON Files (*.json);;All Files (*)"))
        row3_trans = QHBoxLayout()
        row3_trans.addWidget(self.step3_trans_input)
        row3_trans.addWidget(btn3_trans)
        esm_form.addRow("번역 JSON [Step 2 결과, -t, 선택]:", row3_trans)

        self.step3_output = QLineEdit()
        esm_form.addRow("출력 XML [-o, 옵션]:", self.step3_output)
        esm_group.setLayout(esm_form)
        layout.addWidget(esm_group)

        run_esm_btn = QPushButton("▶  Step 3 (ESM 모드) 실행")
        run_esm_btn.clicked.connect(self.run_step3_esm)
        layout.addWidget(run_esm_btn)

        # ── XML 머지 모드 그룹 ─────────────────────────────────────────
        merge_group = QGroupBox("[XML 머지 모드] 완성된 XML + 재번역 JSON → XML <Dest> 업데이트")
        merge_form = QFormLayout()

        self.step3_merge_xml_input = QLineEdit()
        self.step3_merge_xml_input.setPlaceholderText("Step 4/5까지 완성된 .xml 파일")
        btn3_mxml = QPushButton("찾아보기")
        btn3_mxml.clicked.connect(self.browse_step3_merge_xml)
        row3_mxml = QHBoxLayout()
        row3_mxml.addWidget(self.step3_merge_xml_input)
        row3_mxml.addWidget(btn3_mxml)
        merge_form.addRow("기존 XML [-x]:", row3_mxml)

        self.step3_merge_trans_input = QLineEdit()
        self.step3_merge_trans_input.setPlaceholderText("재번역된 translated_dict.json")
        btn3_mtrans = QPushButton("찾아보기")
        btn3_mtrans.clicked.connect(lambda: self.browse_file(self.step3_merge_trans_input, "JSON Files (*.json);;All Files (*)"))
        row3_mtrans = QHBoxLayout()
        row3_mtrans.addWidget(self.step3_merge_trans_input)
        row3_mtrans.addWidget(btn3_mtrans)
        merge_form.addRow("재번역 JSON [-t]:", row3_mtrans)

        self.step3_merge_output = QLineEdit()
        self.step3_merge_output.setPlaceholderText("비워두면 기존 XML 덮어쓰기")
        merge_form.addRow("출력 XML [-o, 옵션]:", self.step3_merge_output)
        merge_group.setLayout(merge_form)
        layout.addWidget(merge_group)

        run_merge_btn = QPushButton("▶  Step 3 (XML 머지 모드) 실행")
        run_merge_btn.clicked.connect(self.run_step3_merge)
        layout.addWidget(run_merge_btn)

        # 공통 정지 버튼
        self.step3_stop_btn = QPushButton("정지")
        self.step3_stop_btn.setEnabled(False)
        self.step3_stop_btn.clicked.connect(self.stop_current_task)
        layout.addWidget(self.step3_stop_btn)

        layout.addStretch()
        return widget

    def browse_step3_esm(self):
        path, _ = QFileDialog.getOpenFileName(self, "ESM/ESP 파일 선택", "", "Bethesda Plugin Files (*.esm *.esp);;All Files (*)")
        if path:
            self.step3_esm_input.setText(path)
            dir_name = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.step3_output.setText(os.path.join(dir_name, f"{base_name}_out.xml"))

    def browse_step3_merge_xml(self):
        path, _ = QFileDialog.getOpenFileName(self, "완성된 XML 파일 선택", "", "XML Files (*.xml);;All Files (*)")
        if path:
            self.step3_merge_xml_input.setText(path)
            # 출력을 기본으로 동일 경로 (덮어쓰기) — 사용자가 원하면 변경 가능
            self.step3_merge_output.setText(path)

    def run_step3_esm(self):
        if not self.step3_esm_input.text():
            QMessageBox.warning(self, "경고", "원본 ESM 파일을 지정하세요.")
            return
        self.run_background_task("Step3", {
            "esm": self.step3_esm_input.text(),
            "trans": self.step3_trans_input.text(),
            "out": self.step3_output.text()
        }, self.step3_stop_btn)

    def run_step3_merge(self):
        if not self.step3_merge_xml_input.text():
            QMessageBox.warning(self, "경고", "기존 XML 파일을 지정하세요.")
            return
        if not self.step3_merge_trans_input.text():
            QMessageBox.warning(self, "경고", "재번역 JSON 파일을 지정하세요.")
            return
        self.run_background_task("Step3", {
            "merge_xml": self.step3_merge_xml_input.text(),
            "trans": self.step3_merge_trans_input.text(),
            "out": self.step3_merge_output.text()
        }, self.step3_stop_btn)

    # 하위 호환: 기존 run_step3 호출 유지
    def run_step3(self):
        self.run_step3_esm()

    # ==========================
    # 5. Step 4 XML 번역 탭
    # ==========================
    def create_step4_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        form = QFormLayout()
        self.step4_input = QLineEdit()
        btn4 = QPushButton("찾아보기")
        btn4.clicked.connect(self.browse_step4_xml)
        row4 = QHBoxLayout()
        row4.addWidget(self.step4_input)
        row4.addWidget(btn4)
        form.addRow("입력 XML (Step 3 결과):", row4)
        
        self.step4_output = QLineEdit()
        self.step4_output.setPlaceholderText("xxx_translated.xml")
        form.addRow("출력 XML:", self.step4_output)
        layout.addLayout(form)
        
        prompt_group = QGroupBox("시스템 프롬프트 (수정가능)")
        p_layout = QVBoxLayout()
        self.step4_prompt_edit = QTextEdit(self.config_mgr.config.get("step4_prompt", ""))
        p_layout.addWidget(self.step4_prompt_edit)
        
        save_prompt4_btn = QPushButton("이 탭의 프롬프트만 config.json에 저장")
        save_prompt4_btn.clicked.connect(self.save_step4_prompt)
        p_layout.addWidget(save_prompt4_btn)
        
        prompt_group.setLayout(p_layout)
        layout.addWidget(prompt_group)
        
        btn_layout = QHBoxLayout()
        run_btn = QPushButton("Step 4 잔여 배치 번역 실행")
        run_btn.clicked.connect(self.run_step4)
        self.step4_stop_btn = QPushButton("정지")
        self.step4_stop_btn.setEnabled(False)
        self.step4_stop_btn.clicked.connect(self.stop_current_task)
        
        btn_layout.addWidget(run_btn)
        btn_layout.addWidget(self.step4_stop_btn)
        layout.addLayout(btn_layout)

        self.step4_ja_check = QCheckBox("일본어 원문 참조하여 번역 (아이템/UI 명칭 뉘앙스)")
        self.step4_ja_check.setChecked(self.config_mgr.config.get("use_ja_ref", False))
        layout.addWidget(self.step4_ja_check)
        
        layout.addStretch()
        return widget

    def browse_step4_xml(self):
        path, _ = QFileDialog.getOpenFileName(self, "XML 파일 선택", "", "XML Files (*.xml);;All Files (*)")
        if path:
            self.step4_input.setText(path)
            dir_name = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.step4_output.setText(os.path.join(dir_name, f"{base_name}_translated.xml"))
            
    def save_step4_prompt(self):
        self.config_mgr.config["step4_prompt"] = self.step4_prompt_edit.toPlainText()
        self.config_mgr.save()
        self.append_log("Step 4 프롬프트 설정이 개별 저장되었습니다.")
        QMessageBox.information(self, "저장 완료", "Step 4 프롬프트가 저장되었습니다.")
        
    def run_step4(self):
        self.config_mgr.config["step4_prompt"] = self.step4_prompt_edit.toPlainText()
        self.config_mgr.save()
        self.run_background_task("Step4", {
            "input": self.step4_input.text(),
            "output": self.step4_output.text(),
            "use_ja_ref": self.step4_ja_check.isChecked()
        }, self.step4_stop_btn)

    # ==========================
    # 6. Step 5 후처리 탭 (스캔 및 선택 번역)
    # ==========================
    def create_step5_tab(self):
        from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QAbstractItemView, QLabel, QHBoxLayout, QWidget
        from PyQt6.QtCore import Qt
        from pathlib import Path
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        form = QFormLayout()
        self.step5_input = QLineEdit()
        self.step5_input.setPlaceholderText("step4 출력 XML 파일")
        btn5 = QPushButton("찾아보기")
        btn5.clicked.connect(self.browse_step5_xml)
        row5 = QHBoxLayout()
        row5.addWidget(self.step5_input)
        row5.addWidget(btn5)
        form.addRow("입력 XML (Step 4 결과):", row5)

        self.step5_output = QLineEdit()
        self.step5_output.setPlaceholderText("비워두면 입력 파일에 덮어씀 (권장)")
        form.addRow("출력 파일명:", self.step5_output)
        layout.addLayout(form)

        # 테이블 추가
        table_label = QLabel("미번역 및 태그 오류 항목 리스트 (스캔 후 표시):")
        layout.addWidget(table_label)
        
        self.step5_table = QTableWidget()
        self.step5_table.setColumnCount(8) # Select, Index, Status, EDID, REC, Error, Source, Dest
        self.step5_table.setHorizontalHeaderLabels(["선택", "Idx", "Status", "EDID", "REC", "Error", "Source", "Dest"])
        self.step5_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.step5_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.step5_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.step5_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        layout.addWidget(self.step5_table)

        btn_layout = QHBoxLayout()
        scan_btn = QPushButton("① 미번역/오류 항목 스캔")
        scan_btn.clicked.connect(self.run_step5_scan)
        
        translate_btn = QPushButton("② 선택한 항목 번역 실행")
        translate_btn.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold;")
        translate_btn.clicked.connect(self.run_step5_translate)
        
        self.step5_stop_btn = QPushButton("정지")
        self.step5_stop_btn.setEnabled(False)
        self.step5_stop_btn.clicked.connect(self.stop_current_task)
        
        btn_layout.addWidget(scan_btn)
        btn_layout.addWidget(translate_btn)
        btn_layout.addWidget(self.step5_stop_btn)
        layout.addLayout(btn_layout)
        
        return widget

    def run_step5_scan(self):
        if not self.step5_input.text():
            QMessageBox.warning(self, "경고", "먼저 입력 XML 파일을 지정하세요.")
            return
        self.step5_table.setRowCount(0)
        self.run_background_task("Step5", {
            "input": self.step5_input.text(),
            "output": self.step5_output.text() or self.step5_input.text(),
            "scan_only": True
        }, self.step5_stop_btn)

    def run_step5_translate(self):
        indices = []
        for r in range(self.step5_table.rowCount()):
            chk_widget = self.step5_table.cellWidget(r, 0)
            if chk_widget:
                chk = chk_widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    indices.append(self.step5_table.item(r, 1).text())
        
        if not indices:
            QMessageBox.warning(self, "경고", "번역할 항목을 하나 이상 선택하세요.")
            return
            
        self.run_background_task("Step5", {
            "input": self.step5_input.text(),
            "output": self.step5_output.text() or self.step5_input.text(),
            "indices": ",".join(indices)
        }, self.step5_stop_btn)

    def browse_step5_xml(self):
        path, _ = QFileDialog.getOpenFileName(self, "XML 파일 선택", "", "XML Files (*.xml);;All Files (*)")
        if path:
            self.step5_input.setText(path)
            dir_name = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            if base_name.endswith("_translated"):
                base_name = base_name.replace("_translated", "")
            self.step5_output.setText(os.path.join(dir_name, f"{base_name}_final.xml"))

    def save_step5_prompt(self):
        self.config_mgr.config["step5_prompt"] = self.step5_prompt_edit.toPlainText()
        self.config_mgr.save()
        self.append_log("Step 5 프롬프트 설정이 개별 저장되었습니다.")
        QMessageBox.information(self, "저장 완료", "Step 5 프롬프트가 저장되었습니다.")

    def run_step5(self):
        mode_map = {
            "전체 (태그 검사 + 미번역 보완)": "all",
            "태그 검사만": "tag_only",
            "미번역 보완만": "translate_only"
        }
        self.config_mgr.config["step5_prompt"] = self.step5_prompt_edit.toPlainText()
        self.config_mgr.save()
        self.run_background_task("Step5", {
            "input": self.step5_input.text(),
            "output": self.step5_output.text(),
            "mode": mode_map.get(self.step5_mode.currentText(), "all")
        }, self.step5_stop_btn)

    # ==========================
    # 스레드 제어 (실행 및 중간 저장 확인)
    # ==========================
    def stop_current_task(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.append_log("강제 정지 시그널을 전송했습니다. (CLI 스크립트 중단 대기 중...)")

    def run_background_task(self, step_name, kwargs, stop_btn):
        # 0. 중복 실행 방지
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "경고", "이미 실행 중인 작업이 있습니다. 현재 작업을 먼저 중지하세요.")
            return

        # 1. 중간 저장 파일(progress) 감지 및 팝업 프롬프트
        # step2, step4, step5는 원본 결과물 앞선 임시 파일 검증
        out_file = kwargs.get("output", "")
        
        progress_file = None
        if out_file and step_name == "Step2":
            progress_file = out_file.replace(".json", ".progress.json")
        elif out_file and step_name in ("Step4", "Step5"):
            # Step5의 경우 출력 파일 확장자가 json일 수도 있음
            ext = os.path.splitext(out_file)[1].lower()
            progress_file = out_file.replace(ext, f".progress{ext}")
            
        has_final = out_file and os.path.exists(out_file)
        has_prog = progress_file and os.path.exists(progress_file)
        
        target_to_delete = out_file
        if has_prog and not has_final:
            target_to_delete = progress_file
            
        if (has_final or has_prog) and step_name in ("Step2", "Step4", "Step5"):
            msg_str = f"기존 작업 결과물({os.path.basename(target_to_delete)})이 존재합니다.\n(또는 이전에 중단된 기록)\n이어하시겠습니까?\n\n[Yes=이어서, No=기존삭제 후 처음부터]"
            reply = QMessageBox.question(
                self, '중간 저장 감지', msg_str,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.No:
                # 삭제 시도 (이어하기 무효화)
                try:
                    if has_final: os.remove(out_file)
                    if has_prog: os.remove(progress_file)
                    self.append_log(f"기존 기록 모두 삭제됨")
                except Exception as e:
                    self.append_log(f"기존 파일 삭제 실패: {e}")
            else:
                self.append_log(f"이어하기 모드로 실행됩니다.")

        self.append_log(f"\n--- {step_name} 시작 ---")
        
        # 윈도우 타이틀 업데이트 (작업 중 표시)
        short_name = ""
        if "input" in kwargs: short_name = os.path.basename(kwargs["input"])
        elif "esm" in kwargs: short_name = os.path.basename(kwargs["esm"])
        self.setWindowTitle(f"[{step_name} 작업 중...] {short_name} - {self.base_title}")
        
        stop_btn.setEnabled(True)
        self.current_stop_btn = stop_btn
        self.current_kwargs = kwargs
        
        self.worker = WorkerThread(step_name, kwargs)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.on_task_finished)
        self.worker.start()

    def closeEvent(self, event):
        """창이 닫힐 때 실행 중인 스레드가 있으면 안전하게 중단"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, '작업 진행 중', 
                '현재 번역 작업이 진행 중입니다. 정말 종료하시겠습니까?\n(중단 시 진행 데이터가 유실되지는 않지만, 작업 중인 청크는 유실될 수 있습니다.)',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
                QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self.worker.stop()
                self.worker.wait(3000) # 최대 3초 대기
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def on_task_finished(self, success, msg, step_name):
        self.setWindowTitle(self.base_title)  # 타이틀 복구
        if hasattr(self, 'current_stop_btn') and self.current_stop_btn:
            self.current_stop_btn.setEnabled(False)
        if success:
            self.append_log(f"성공: {msg}")
            if step_name == "Step2" and getattr(self, "current_kwargs", {}).get("profile_only"):
                self.load_step2_profile_file()
        else:
            self.append_log(f"실패/에러: {msg}")

    # ==========================
    # 8. Step 6: 외부 모드/DLC 번역 교정
    # ==========================
    def create_step6_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        group = QGroupBox("입출력 파일 및 모드 설정")
        form = QFormLayout(group)

        self.s6_input_path = QLineEdit()
        btn_in = QPushButton("파일 찾기")
        btn_in.clicked.connect(self.browse_step6_input)
        row1 = QHBoxLayout()
        row1.addWidget(self.s6_input_path)
        row1.addWidget(btn_in)
        form.addRow("입력 파일 (.xml/.strings):", row1)
        
        self.s6_mode_cb = QComboBox()
        self.s6_mode_cb.addItems(["update (신규 번역 추가)", "refine (어투 교정)"])
        form.addRow("작업 모드 선택:", self.s6_mode_cb)

        self.s6_profile_path = QLineEdit()
        btn_prof = QPushButton("파일 찾기")
        btn_prof.clicked.connect(self.browse_step6_profile)
        row2 = QHBoxLayout()
        row2.addWidget(self.s6_profile_path)
        row2.addWidget(btn_prof)
        form.addRow("[선택] 어투 프로파일 (Refine용):", row2)
        
        self.s6_ref_path = QLineEdit()
        btn_ref = QPushButton("파일 찾기")
        btn_ref.clicked.connect(self.browse_step6_ref)
        row3 = QHBoxLayout()
        row3.addWidget(self.s6_ref_path)
        row3.addWidget(btn_ref)
        form.addRow("[선택] 참조 기존번역 (Update용):", row3)

        self.s6_output_path = QLineEdit("mod_translated.xml")
        btn_out = QPushButton("저장 경로")
        btn_out.clicked.connect(lambda: self.browse_save_file(self.s6_output_path, "XML Files (*.xml)"))
        row4 = QHBoxLayout()
        row4.addWidget(self.s6_output_path)
        row4.addWidget(btn_out)
        form.addRow("출력 대상 XML 파일:", row4)

        layout.addWidget(group)
        
        prompt_group = QGroupBox("Step 6 전용 프롬프트 (Refine / Update)")
        p_layout = QVBoxLayout(prompt_group)
        self.s6_refine_prompt = QTextEdit(self.config_mgr.config.get("step6_refine_prompt", "당신은 게임 전문 번역 교정자입니다. 원문과 번역문이 주어지면, 직역체나 오락가락하는 어투(존댓말/반말 혼용)를 일관성 있고 매끄러운 한국어로 교정하세요."))
        self.s6_refine_prompt.setMaximumHeight(60)
        self.s6_update_prompt = QTextEdit(self.config_mgr.config.get("step6_update_prompt", "당신은 게임 전문 번역가입니다. 제공된 주변 문맥(기존 번역본)을 참고하여, 새롭게 추가된 원문들의 톤앤매너를 기존 번역과 일치하게 번역하세요."))
        self.s6_update_prompt.setMaximumHeight(60)
        
        p_layout.addWidget(QLabel("어투 교정 (Refine) 프롬프트:"))
        p_layout.addWidget(self.s6_refine_prompt)
        p_layout.addWidget(QLabel("모드 업데이트 (Update) 프롬프트:"))
        p_layout.addWidget(self.s6_update_prompt)
        
        btn_save_p = QPushButton("프롬프트 저장")
        btn_save_p.clicked.connect(self.save_s6_prompts)
        p_layout.addWidget(btn_save_p)
        layout.addWidget(prompt_group)

        btn_run = QPushButton("Step 6 작업 시작")
        btn_run.setMinimumHeight(40)
        self.s6_stop_btn = QPushButton("작업 중지")
        self.s6_stop_btn.setEnabled(False)

        btn_run.clicked.connect(self.run_step6)
        self.s6_stop_btn.clicked.connect(self.stop_current_task)

        h_btn = QHBoxLayout()
        h_btn.addWidget(btn_run)
        h_btn.addWidget(self.s6_stop_btn)
        layout.addLayout(h_btn)

        layout.addStretch()
        return widget
        
    def save_s6_prompts(self):
        self.config_mgr.config["step6_refine_prompt"] = self.s6_refine_prompt.toPlainText().strip()
        self.config_mgr.config["step6_update_prompt"] = self.s6_update_prompt.toPlainText().strip()
        self.config_mgr.save()
        self.append_log("Step 6 프롬프트가 저장되었습니다.")

    def browse_step6_input(self):
        path, _ = QFileDialog.getOpenFileName(self, "입력 파일 선택", "", "XML/Strings Files (*.xml *.strings *.dlstrings *.ilstrings)")
        if path:
            self.s6_input_path.setText(path)
            dir_name = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0]
            self.s6_output_path.setText(os.path.join(dir_name, f"{base_name}_translated.xml"))
            
    def browse_step6_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "프로파일 선택", "", "JSON Files (*_profile.json)")
        if path:
            self.s6_profile_path.setText(path)
            
    def browse_step6_ref(self):
        path, _ = QFileDialog.getOpenFileName(self, "참조 파일 선택", "", "XML/Strings Files (*.xml *.strings *.dlstrings *.ilstrings)")
        if path:
            self.s6_ref_path.setText(path)

    def run_step6(self):
        inp = self.s6_input_path.text().strip()
        out = self.s6_output_path.text().strip()
        mode_text = self.s6_mode_cb.currentText()
        mode = "refine" if "refine" in mode_text else "update"
        
        prof = self.s6_profile_path.text().strip()
        ref = self.s6_ref_path.text().strip()

        if not inp or not out:
            QMessageBox.warning(self, "경고", "입력 파일과 출력 파일 경로를 지정하세요.")
            return

        kwargs = {"input": inp, "output": out, "mode": mode}
        if prof and mode == "refine": kwargs["profile"] = prof
        if ref and mode == "update": kwargs["reference"] = ref

        self.current_stop_btn = self.s6_stop_btn
        self.run_background_task("Step6", kwargs, self.s6_stop_btn)

    # ==========================
    # 9. 멀티모달 오디오 오디션 탭
    # ==========================
    def create_audio_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        extract_group = QGroupBox("1. 보이스 파일 추출 (extract_audio.py)")
        e_layout = QFormLayout()

        self.audio_priority_input = QLineEdit()
        self.audio_priority_input.setPlaceholderText("Step 1 결과물과 동일 폴더의 priority_list.json")
        btn_p = QPushButton("찾아보기")
        btn_p.clicked.connect(lambda: self.browse_file(self.audio_priority_input, "JSON Files (priority_list.json);;All Files (*)"))
        row_p = QHBoxLayout()
        row_p.addWidget(self.audio_priority_input)
        row_p.addWidget(btn_p)
        e_layout.addRow("화자 우선순위 목록 (-p):", row_p)

        self.audio_data_dir = QLineEdit(self.config_mgr.config.get("game_data_dir", ""))
        btn_d = QPushButton("폴더 선택")
        btn_d.clicked.connect(lambda: self.browse_folder(self.audio_data_dir))
        row_d = QHBoxLayout()
        row_d.addWidget(self.audio_data_dir)
        row_d.addWidget(btn_d)
        e_layout.addRow("스타필드 Data 폴더 (-d):", row_d)

        self.audio_temp_dir = QLineEdit("temp/audition")
        e_layout.addRow("임시 추출 작업 폴더 (-o):", self.audio_temp_dir)

        extract_group.setLayout(e_layout)
        layout.addWidget(extract_group)

        btn_e_run = QPushButton("보이스 샘플 추출 실행")
        btn_e_run.clicked.connect(self.run_audio_extract)
        layout.addWidget(btn_e_run)

        profile_group = QGroupBox("2. Gemini 멀티모달 어투 분석 (audition_profiler.py)")
        p_layout = QFormLayout()

        self.audio_profile_out = QLineEdit()
        self.audio_profile_out.setPlaceholderText("tone_profiles.json (Step 2에서 참조됨)")
        p_layout.addRow("최종 프로파일 저장 경로 (-o):", self.audio_profile_out)

        profile_group.setLayout(p_layout)
        layout.addWidget(profile_group)

        btn_p_run = QPushButton("오디오 분석 및 어투 가이드 생성 실행 (Gemini 필요)")
        btn_p_run.setStyleSheet("background-color: #1565c0; color: white; font-weight: bold;")
        btn_p_run.clicked.connect(self.run_audio_profile)
        layout.addWidget(btn_p_run)

        self.audio_stop_btn = QPushButton("정지")
        self.audio_stop_btn.setEnabled(False)
        self.audio_stop_btn.clicked.connect(self.stop_current_task)
        layout.addWidget(self.audio_stop_btn)

        layout.addStretch()
        return widget

    def run_audio_extract(self):
        if not self.audio_priority_input.text():
            QMessageBox.warning(self, "경고", "먼저 priority_list.json 파일을 지정하세요.")
            return
        if not self.audio_data_dir.text():
            QMessageBox.warning(self, "경고", "스타필드 Data 폴더를 지정하세요.")
            return

        # 마지막 사용된 Data 폴더 경로 저장
        self.config_mgr.config["game_data_dir"] = self.audio_data_dir.text()
        self.config_mgr.save()

        self.run_background_task("AudioExtract", {
            "priority_list": self.audio_priority_input.text(),
            "data_dir": self.audio_data_dir.text(),
            "output_dir": self.audio_temp_dir.text()
        }, self.audio_stop_btn)

    def run_audio_profile(self):
        if not self.audio_priority_input.text():
            QMessageBox.warning(self, "경고", "먼저 priority_list.json 파일을 지정하세요.")
            return

        # 출력 파일이 없으면 priority_list와 같은 폴더에 tone_profiles.json으로 설정
        if not self.audio_profile_out.text():
            p_dir = os.path.dirname(self.audio_priority_input.text())
            self.audio_profile_out.setText(os.path.join(p_dir, "tone_profiles.json"))

        self.run_background_task("AudioProfile", {
            "config": CONFIG_FILE,
            "priority_list": self.audio_priority_input.text(),
            "audition_dir": self.audio_temp_dir.text(),
            "output": self.audio_profile_out.text()
        }, self.audio_stop_btn)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainApp()
    window.show()
    sys.exit(app.exec())
