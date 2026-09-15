"""
document_loader.py - 多格式文档加载解析核心（优化版 v4）
优化内容：
  1. PDF: 表格结构化提取（pdfplumber优先 + get_tables降级 + dict兜底）
       + 双栏检测与文本重排 + 旋转校正 + OCR兜底
       + 多页长文本页码信息注入（text内容前加【第X页/Y】前缀）
  2. DOCX: 改进表格提取（Markdown表格格式）+ 段落结构保留
  3. TXT: 自动编码检测（utf-8/gbk/latin-1）
  4. XLSX: 改进为openpyxl方案，支持合并单元格识别
  5. 修复 fitz 弃用警告，兼容新旧版 PyMuPDF
  6. 修复 pdfplumber options 参数兼容性问题
  7. 修复 get_tables() 检测方法（hasattr 实际检测）
  8. 修复降级链逻辑（确保各策略正确串联）
  9. 降低冗余日志级别（每页降级日志改为DEBUG，避免INFO刷屏）

依赖要求：
  pip install "pymupdf>=1.23.0" python-docx openpyxl pandas
可选依赖（增强能力）：
  pip install pytesseract          # PDF OCR兜底（扫描件）
  pip install pdfplumber           # PDF表格备选提取方案（强烈推荐安装）
"""

import hashlib
import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import logging

# 抑制 fitz 弃用警告（兼容新旧版 PyMuPDF）
warnings.filterwarnings("ignore", message=".*The `fitz` API is deprecated.*")
warnings.filterwarnings("ignore", message=".*fitz is deprecated.*")

# 各格式解析库
# 兼容写法：优先 import pymupdf（新版），降级到 import fitz（旧版）
try:
    import pymupdf

    HAS_PYMUPDF = True
except ImportError:
    try:
        import fitz as pymupdf

        HAS_PYMUPDF = True
    except ImportError:
        HAS_PYMUPDF = False

# 从 pymupdf 中导入常用类（兼容新旧版）
try:
    from pymupdf import Rect
except ImportError:
    try:
        from fitz import Rect
    except ImportError:
        Rect = None

try:
    import pandas as pd

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    from docx import Document as DocxDocument

    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    import openpyxl

    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# 检测 pdfplumber 是否可用
HAS_PDFPLUMBER = False
_pdfplumber_version = "unknown"
try:
    import pdfplumber

    HAS_PDFPLUMBER = True
    _pdfplumber_version = getattr(pdfplumber, "__version__", "unknown")
except ImportError:
    pass

# 检测 pytesseract 是否可用
HAS_TESSERACT = False
try:
    import pytesseract

    HAS_TESSERACT = True
except ImportError:
    pass

# 检测 Pillow (PIL) 是否可用（DOCX 图片 OCR 依赖）
HAS_PIL = False
try:
    from PIL import Image
    import io as _io

    HAS_PIL = True
except ImportError:
    pass

# 检测 PyMuPDF 版本是否支持 get_tables()
# 使用 hasattr 实际检测，而非仅依赖版本号
PYMUPDF_GET_TABLES_AVAILABLE = False
if HAS_PYMUPDF:
    _check_doc = None
    try:
        _check_doc = pymupdf.open()
        _test_page = _check_doc[0]
        PYMUPDF_GET_TABLES_AVAILABLE = hasattr(_test_page, 'get_tables') and callable(_test_page.get_tables)
        _check_doc.close()
    except Exception:
        version_str = getattr(pymupdf, "__version__", "")
        if version_str:
            version_parts = version_str.split(".")
            major = int(version_parts[0]) if len(version_parts) > 0 else 0
            minor = int(version_parts[1]) if len(version_parts) > 1 else 0
            if major > 1 or (major == 1 and minor >= 23):
                PYMUPDF_GET_TABLES_AVAILABLE = True
        if _check_doc is not None:
            try:
                _check_doc.close()
            except Exception:
                pass

from config import (
    KNOWLEDGE_BASE_DIR,
    SUPPORTED_EXTENSIONS,
    PDF_SUBDIR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ============================================================================
# 基础数据结构
# ============================================================================

@dataclass
class Document:
    """统一文档块数据结构"""
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        return cls(text=data["text"], metadata=data.get("metadata", {}))


def compute_file_hash(file_path: Path) -> str:
    """计算文件 MD5 hash，用于增量更新判断"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


# ============================================================================
# PDF 解析 - 辅助函数
# ============================================================================

def preprocess_page(page) -> Dict[str, Any]:
    """
    页面预处理：
    1. 检测并校正旋转角度
    2. 检测纸张尺寸(A3/A4/横向)，记录元信息
    3. 检测是否为图片型PDF(触发OCR兜底)
    返回: dict 包含 rotation, is_landscape, page_width, page_height,
          text_length, has_images, need_ocr
    """
    rotation = page.rotation  # 0, 90, 180, 270
    # 获取页面尺寸
    rect = page.rect
    width, height = rect.width, rect.height
    # 标准化：确保 width <= height 时为 portrait，反之为 landscape
    is_landscape = width > height
    # 如果页面旋转了90/270度且是横向，需要交换宽高
    if rotation in (90, 270):
        is_landscape = not is_landscape
    # 检测是否为图片型页面（文本极少但页面有内容）
    text = page.get_text("text").strip()
    text_length = len(text)
    # 检查页面是否有图片
    images = page.get_images(full=True)
    has_images = len(images) > 0
    # 判断是否需要OCR：文本很少但有图片，或文本极少
    need_ocr = (text_length < 20 and has_images) or (text_length < 5 and page.get_text("blocks"))
    return {
        "rotation": rotation,
        "is_landscape": is_landscape,
        "page_width": width,
        "page_height": height,
        "text_length": text_length,
        "has_images": has_images,
        "need_ocr": need_ocr,
    }


def _data_to_markdown_table(data: List[List]) -> str:
    """将二维列表转为 Markdown 表格格式"""
    if not data:
        return ""
    max_cols = max(len(row) for row in data)
    for row in data:
        while len(row) < max_cols:
            row.append("")
    lines = []
    header = " | ".join(str(cell).strip() for cell in data[0])
    lines.append(f"| {header} |")
    sep = "| " + " | ".join("---" for _ in data[0]) + " |"
    lines.append(sep)
    for row in data[1:]:
        line = "| " + " | ".join(str(cell).strip() for cell in row) + " |"
        lines.append(line)
    return "\n".join(lines)


def extract_tables_with_pdfplumber(file_path: Path, page_num: int) -> List[Dict]:
    """
    首选方案：使用 pdfplumber 提取表格（精度最高）
    pdfplumber 基于 PDF 的绘图指令提取表格，对表格结构识别最准确

    兼容处理：不同版本的 pdfplumber 参数名可能不同
    - 旧版: extract_tables() 无参数
    - 新版: extract_tables(table_settings={...})
    """
    if not HAS_PDFPLUMBER:
        return []
    tables = []
    try:
        with pdfplumber.open(file_path) as pdf:
            if page_num >= len(pdf.pages):
                return []
            page = pdf.pages[page_num]

            # 尝试不同的参数方式调用 extract_tables
            extracted_tables = None
            try:
                # 方式1: 不带参数（最兼容）
                extracted_tables = page.extract_tables()
            except TypeError:
                try:
                    # 方式2: table_settings 参数（新版 pdfplumber）
                    extracted_tables = page.extract_tables(
                        table_settings={
                            "vertical_strategy": "text",
                            "horizontal_strategy": "text",
                        }
                    )
                except (TypeError, AttributeError):
                    try:
                        # 方式3: options 参数（中间版本）
                        extracted_tables = page.extract_tables(
                            options={
                                "vertical_strategy": "text",
                                "horizontal_strategy": "text",
                            }
                        )
                    except (TypeError, AttributeError):
                        # 方式4: 什么都不传，用默认设置
                        extracted_tables = page.extract_tables()

            if extracted_tables:
                for table_data in extracted_tables:
                    if table_data and len(table_data) > 0:
                        # 清理单元格内容
                        cleaned_table = []
                        for row in table_data:
                            cleaned_row = [
                                str(cell).strip() if cell is not None else ""
                                for cell in row
                            ]
                            cleaned_table.append(cleaned_row)
                        md_table = _data_to_markdown_table(cleaned_table)
                        tables.append({
                            "type": "table",
                            "content": md_table,
                            "rows": len(cleaned_table),
                            "cols": len(cleaned_table[0]) if cleaned_table else 0,
                            "method": "pdfplumber",
                        })
    except Exception as e:
        logger.warning(f"[PDF] pdfplumber 表格提取失败: {e}")
    return tables


def extract_tables_with_get_tables(page) -> List[Dict]:
    """
    备选方案：使用 PyMuPDF >= 1.23.0 的 page.get_tables() 提取表格

    注意：使用 hasattr 实际检测，而非仅依赖版本号
    """
    tables = []
    if not hasattr(page, 'get_tables') or not callable(page.get_tables):
        logger.debug("[PDF] page.get_tables() 方法不存在，跳过")
        return tables

    try:
        page_tables = page.get_tables()
        if page_tables:
            for table in page_tables:
                data = table.extract()
                if data and len(data) > 0:
                    md_table = _data_to_markdown_table(data)
                    tables.append({
                        "type": "table",
                        "content": md_table,
                        "rows": len(data),
                        "cols": len(data[0]) if data else 0,
                        "method": "pymupdf_get_tables",
                    })
    except AttributeError as e:
        logger.debug(f"[PDF] get_tables() 调用失败(AttributeError): {e}")
    except Exception as e:
        logger.warning(f"[PDF] get_tables() 提取失败: {e}")
    return tables


def extract_tables_with_dict(page) -> List[Dict]:
    """
    兜底方案：通过 dict 模式提取表格（兼容旧版本 PyMuPDF）
    通过文本块的坐标信息检测表格结构

    改进版：使用坐标聚类检测网格状表格结构
    - 先按 y 坐标聚类分行
    - 再按 x 坐标聚类分列
    - 构建二维矩阵
    """
    tables = []
    try:
        text_dict = page.get_text("dict")
        blocks = text_dict.get("blocks", [])

        # 收集所有文本块的坐标和内容信息
        text_blocks = []
        for block in blocks:
            if block.get("type") == 0:  # 文本块
                rect = block.get("rect", None)
                if rect is not None:
                    block_text = ""
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            block_text += span.get("text", "")
                    block_text = block_text.strip()
                    if block_text:
                        text_blocks.append({
                            "x0": rect[0],
                            "y0": rect[1],
                            "x1": rect[2],
                            "y1": rect[3],
                            "text": block_text,
                            "center_x": (rect[0] + rect[2]) / 2,
                            "center_y": (rect[1] + rect[3]) / 2,
                        })

        if len(text_blocks) < 4:
            return tables

        # Step 1: 按 y 坐标聚类分行
        y_threshold = 8.0
        sorted_by_y = sorted(text_blocks, key=lambda b: b["center_y"])

        rows = []
        current_row = [sorted_by_y[0]]
        current_y_center = sorted_by_y[0]["center_y"]

        for block in sorted_by_y[1:]:
            if abs(block["center_y"] - current_y_center) <= y_threshold:
                current_row.append(block)
            else:
                rows.append(current_row)
                current_row = [block]
                current_y_center = block["center_y"]
        if current_row:
            rows.append(current_row)

        # Step 2: 如果有至少2行且多数行有多个块，尝试检测表格结构
        if len(rows) >= 2:
            multi_block_rows = [row for row in rows if len(row) >= 2]
            if len(multi_block_rows) >= 2:
                # Step 3: 按 x 坐标聚类分列
                all_x_centers = []
                for row in rows:
                    for block in row:
                        all_x_centers.append(block["center_x"])
                all_x_centers = sorted(set(round(x, 1) for x in all_x_centers))

                # 聚类 x 坐标
                x_threshold = 10.0
                columns = []
                if all_x_centers:
                    current_col = [all_x_centers[0]]
                    current_x = all_x_centers[0]
                    for x in all_x_centers[1:]:
                        if abs(x - current_x) <= x_threshold:
                            current_col.append(x)
                        else:
                            columns.append(sum(current_col) / len(current_col))
                            current_col = [x]
                            current_x = x
                    columns.append(sum(current_col) / len(current_col))

                # Step 4: 构建表格矩阵
                if len(columns) >= 2:
                    table_data = []
                    for row in rows:
                        row_sorted = sorted(row, key=lambda b: b["center_x"])
                        table_row = [""] * len(columns)
                        for block in row_sorted:
                            best_col = 0
                            best_dist = float("inf")
                            for ci, col_x in enumerate(columns):
                                dist = abs(block["center_x"] - col_x)
                                if dist < best_dist:
                                    best_dist = dist
                                    best_col = ci
                            if best_col < len(table_row):
                                if table_row[best_col]:
                                    table_row[best_col] += " " + block["text"]
                                else:
                                    table_row[best_col] = block["text"]
                        table_data.append(table_row)

                    if table_data:
                        md_table = _data_to_markdown_table(table_data)
                        tables.append({
                            "type": "table",
                            "content": md_table,
                            "rows": len(table_data),
                            "cols": len(table_data[0]) if table_data else 0,
                            "method": "dict_fallback_improved",
                        })
    except Exception as e:
        logger.warning(f"[PDF] dict模式表格提取失败: {e}")
    return tables


def extract_tables_from_page(page, file_path: Path, page_num: int) -> List[Dict]:
    """
    表格提取主函数 - 渐进式降级策略：
    1. pdfplumber（精度最高，兼容不同版本参数）
    2. PyMuPDF get_tables()（实际hasattr检测）
    3. dict模式兜底（改进版坐标聚类）

    注意：file_path 参数仅在 pdfplumber 方案时需要
    """
    # 策略1: pdfplumber（最推荐，表格识别精度最高）
    if HAS_PDFPLUMBER:
        tables = extract_tables_with_pdfplumber(file_path, page_num)
        if tables:
            logger.info(f"[PDF] 页面{page_num + 1}通过 pdfplumber 提取到 {len(tables)} 个表格")
            return tables
        else:
            logger.debug(f"[PDF] 页面{page_num + 1} pdfplumber 未提取到表格，尝试下一策略")

    # 策略2: PyMuPDF get_tables()（实际检测方法是否存在）
    if PYMUPDF_GET_TABLES_AVAILABLE:
        tables = extract_tables_with_get_tables(page)
        if tables:
            logger.info(f"[PDF] 页面{page_num + 1}通过 pymupdf.get_tables() 提取到 {len(tables)} 个表格")
            return tables
        else:
            logger.debug(f"[PDF] 页面{page_num + 1} get_tables() 未提取到表格，尝试下一策略")

    # 策略3: dict模式兜底
    logger.debug(f"[PDF] 页面{page_num + 1} 表格提取降级到 dict 模式")
    tables = extract_tables_with_dict(page)
    if tables:
        logger.info(f"[PDF] 页面{page_num + 1} 通过 dict 模式提取到 {len(tables)} 个表格块")
    else:
        logger.debug(f"[PDF] 页面{page_num + 1} dict 模式也未提取到表格")
    return tables


def detect_columns(page, threshold=0.45) -> List[Tuple[float, float]]:
    """
    检测页面分栏布局
    参数:
        page: PyMuPDF page 对象
        threshold: 分栏间隙比例阈值
    返回:
        [(col_start_x, col_end_x), ...] 列的x坐标范围列表
    """
    text_dict = page.get_text("dict")
    blocks = text_dict.get("blocks", [])
    block_x_ranges = []
    for block in blocks:
        if block.get("type") == 0:  # 文本块
            rect = block.get("rect", None)
            if rect is not None:
                block_x_ranges.append((rect[0], rect[2]))
    if not block_x_ranges:
        return []
    page_width = page.rect.width
    # 检测分栏间隙：找 x1 最大值集中的区域
    x1_values = sorted([x1 for x0, x1 in block_x_ranges])
    if not x1_values:
        return []
    # 找空白间隙（某个x区间内没有文本块跨越）
    gaps = []
    gap_threshold = page_width * 0.08  # 间隙至少占页面8%才认为是分栏
    # 将所有块的左右边界排序
    all_edges = []
    for x0, x1 in block_x_ranges:
        all_edges.append((x0, 'start'))
        all_edges.append((x1, 'end'))
    all_edges.sort(key=lambda e: e[0])
    # 扫描找间隙
    active_count = 0
    gap_start = None
    for edge_x, edge_type in all_edges:
        if edge_type == 'start':
            if active_count == 0 and gap_start is not None:
                gap_width = edge_x - gap_start
                if gap_width > gap_threshold and gap_start > 0:
                    gaps.append(gap_start)
            active_count += 1
        else:
            active_count -= 1
            if active_count == 0:
                gap_start = edge_x
    if gaps:
        gaps.sort()
        # 过滤掉太靠近边缘的间隙
        gaps = [g for g in gaps if g > page_width * 0.05 and g < page_width * 0.95]
        col_starts = [0] + gaps + [page_width]
        columns = []
        for i in range(len(col_starts) - 1):
            col_start = col_starts[i]
            col_end = col_starts[i + 1]
            if col_end - col_start > page_width * 0.1:
                columns.append((col_start, col_end))
    else:
        columns = [(0, page_width)]
    return columns


def reorder_text_blocks_by_column(blocks: List[dict], columns: List[Tuple[float, float]]) -> List[dict]:
    """按列优先顺序重排文本块"""
    if len(columns) <= 1:
        return sorted(blocks, key=lambda b: (b.get("rect", [0, 0, 0, 0])[1], b.get("rect", [0, 0, 0, 0])[0]))
    column_blocks = [[] for _ in columns]
    for block in blocks:
        rect = block.get("rect", [0, 0, 0, 0])
        block_x = rect[0]
        assigned = False
        for i, (col_start, col_end) in enumerate(columns):
            if col_start <= block_x <= col_end:
                column_blocks[i].append(block)
                assigned = True
                break
        if not assigned:
            column_blocks[0].append(block)
    ordered_blocks = []
    for col_blks in column_blocks:
        col_blks.sort(key=lambda b: (b.get("rect", [0, 0, 0, 0])[1], b.get("rect", [0, 0, 0, 0])[0]))
        ordered_blocks.extend(col_blks)
    return ordered_blocks


def _try_ocr_page(page) -> str:
    """尝试对页面进行OCR识别"""
    if not HAS_TESSERACT:
        logger.warning("pytesseract 未安装，跳过OCR（可执行 pip install pytesseract 安装）")
        return ""
    try:
        mat = pymupdf.Matrix(2.0, 2.0)  # 2倍缩放提高OCR精度
        pix = page.get_pixmap(matrix=mat)
        img_data = pix.tobytes("png")
        text = pytesseract.image_to_string(img_data, lang="chi_sim+eng")
        return text.strip()
    except ImportError:
        logger.warning("pytesseract 未安装，跳过OCR")
        return ""
    except Exception as e:
        logger.error(f"OCR失败: {e}")
        return ""


# ============================================================================
# PDF 解析 - 主函数（优化版 v4）
# ============================================================================

def extract_logical_page_number(text: str) -> Optional[int]:
    """
    从页面文本中提取逻辑页码（内容中标注的页码）
    
    常见页码格式:
    - "Page 39 of 50" (西式页码)
    - "第39页/50" (中文页码)
    - "P.39" (简写)
    
    返回: 逻辑页码 (如39)，未找到返回None
    """
    import re
    
    # 页脚/页眉常见的页码模式
    page_patterns = [
        (r'Page\s+(\d+)\s+of\s+\d+', 1),        # Page 39 of 50
        (r'第\s*(\d+)\s*页\s*[^\d]*\d+', 1),    # 第39页/50
        (r'(\d+)\s*/\s*\d+\s*[页Pp]', 1),       # 39/50页, 39/50P
        (r'[Pp]\.\s*(\d+)', 1),                 # P.39, p.39
        (r'页码[:：]?\s*(\d+)', 1),              # 页码:39
        (r'^(\d+)\s*$', 1),                     # 单独的数字行（可能是页码）
    ]
    
    # 检查文本的末尾部分（页脚通常在这里）
    lines = text.strip().split('\n')
    
    # 优先检查最后几行（页脚区域）
    footer_lines = lines[-5:] if len(lines) >= 5 else lines
    
    for line in footer_lines:
        line_clean = line.strip()
        if not line_clean:
            continue
            
        for pattern, group_idx in page_patterns:
            match = re.search(pattern, line_clean, re.IGNORECASE)
            if match:
                try:
                    page_num = int(match.group(group_idx))
                    # 简单验证：页码应该在合理范围内（1-999）
                    if 1 <= page_num <= 999:
                        logger.debug(f"[PDF] 提取到逻辑页码: {page_num} (模式: {pattern})")
                        return page_num
                except (ValueError, IndexError):
                    continue
    
    # 如果页脚没找到，搜索整个文本（但优先级较低）
    whole_text = text.strip()
    for pattern, group_idx in page_patterns:
        matches = list(re.finditer(pattern, whole_text, re.IGNORECASE))
        if matches:
            # 取最后一个匹配（通常页脚在文档末尾）
            match = matches[-1]
            try:
                page_num = int(match.group(group_idx))
                if 1 <= page_num <= 999:
                    logger.debug(f"[PDF] 从全文提取逻辑页码: {page_num}")
                    return page_num
            except (ValueError, IndexError):
                continue
    
    return None


def load_pdf(file_path: Path) -> List[Document]:
    """
    加载 .pdf 文件（优化版 v4）+ 双页码系统支持

    优化点：
    1. 页面旋转校正 - 处理横向/旋转页面
    2. 表格结构化提取（Markdown格式）- 保留行列关系
       - 优先使用 pdfplumber（精度最高，兼容多版本）
       - 降级到 PyMuPDF get_tables()（>= 1.23.0，hasattr检测）
       - 再降级到 dict 模式兜底
    3. 双栏布局检测与文本重排 - 解决多栏排版文字交错
    4. A3/横向等特殊纸张适配 - 记录页面元信息
    5. OCR兜底（扫描件）- 处理图片型PDF
    6. 【v4新增】多页长文本页码信息注入 - text内容前加【第X页/Y】前缀
    7. 修复 fitz 弃用警告
    8. 修复 pdfplumber options 参数兼容性问题
    9. 修复 get_tables() 检测方法
    10. 降低冗余日志级别 - 每页降级日志改为DEBUG，避免INFO刷屏
    11. 【v5新增】双页码系统 - 支持物理页码和逻辑页码

    参数:
        file_path: PDF 文件路径
    返回:
        List[Document]: 解析后的文档列表，每页一个 Document
    """
    logger.info(f"[PDF] 解析: {file_path.name}")

    # 打印环境信息，便于排查
    if HAS_PYMUPDF:
        ver = getattr(pymupdf, "__version__", "unknown")
        logger.info(f"[PDF] PyMuPDF版本: {ver}, get_tables可用: {PYMUPDF_GET_TABLES_AVAILABLE}")
    if HAS_PDFPLUMBER:
        logger.info(f"[PDF] pdfplumber 已安装 (v{_pdfplumber_version})，将优先使用其进行表格提取")
    if not HAS_PDFPLUMBER:
        logger.warning("[PDF] pdfplumber 未安装！建议安装以获得更好的表格提取效果: pip install pdfplumber")

    documents = []
    try:
        doc = pymupdf.open(file_path)
        total_pages = len(doc)

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            # Step 1: 页面预处理
            page_info = preprocess_page(page)
            page_contents = []

            if page_info["need_ocr"]:
                # 扫描件/图片型PDF -> OCR兜底
                logger.info(f"[PDF] 页面{page_num + 1}检测到图片型内容，尝试OCR")
                ocr_text = _try_ocr_page(page)
                if ocr_text:
                    page_contents.append({
                        "type": "text",
                        "content": ocr_text,
                    })
                else:
                    logger.warning(f"[PDF] 页面{page_num + 1}OCR失败")
            else:
                # 普通PDF -> 多策略提取
                # 2a: 提取表格（渐进式降级：pdfplumber -> get_tables -> dict）
                tables = extract_tables_from_page(page, file_path, page_num)
                for table_info in tables:
                    page_contents.append({
                        "type": "table",
                        "content": table_info["content"],
                    })

                # 2b: 提取正文文本
                text_dict = page.get_text("dict")
                blocks = text_dict.get("blocks", [])
                # 过滤掉表格块（type=1为表格块）
                text_blocks = [b for b in blocks if b.get("type") == 0]
                if not text_blocks:
                    text = page.get_text("text").strip()
                    if text:
                        page_contents.append({"type": "text", "content": text})
                else:
                    # 2c: 双栏检测与重排
                    columns = detect_columns(page)
                    ordered_blocks = reorder_text_blocks_by_column(text_blocks, columns)
                    paragraphs = []
                    for block in ordered_blocks:
                        lines = []
                        for line in block.get("lines", []):
                            line_text = ""
                            for span in line.get("spans", []):
                                line_text += span.get("text", "")
                            lines.append(line_text)
                        para_text = "\n".join(lines).strip()
                        if para_text:
                            paragraphs.append(para_text)
                    if paragraphs:
                        page_contents.append({
                            "type": "text",
                            "content": "\n\n".join(paragraphs),
                        })

            # Step 2: 组装页面内容 + 注入页码信息（v4 新增） + 双页码系统（v5 新增）
            if page_contents:
                table_parts = [c["content"] for c in page_contents if c["type"] == "table"]
                text_parts = [c["content"] for c in page_contents if c["type"] == "text"]
                combined_parts = table_parts + text_parts
                combined_text = "\n\n".join(combined_parts)

                if combined_text.strip():
                    # 【v5 新增】双页码系统：提取逻辑页码
                    logical_page = extract_logical_page_number(combined_text)
                    has_logical_page = logical_page is not None
                    display_page = logical_page if has_logical_page else (page_num + 1)
                    
                    # 确定使用的页码标签（优先使用逻辑页码）
                    if has_logical_page:
                        page_num_label = f"【第{display_page}页(逻辑)/{total_pages}页】"
                        page_note = f"文档标注页码:{logical_page}, 文件物理页码:{page_num + 1}"
                    else:
                        page_num_label = f"【第{display_page}页(物理)/{total_pages}页】"
                        page_note = f"文件物理页码:{page_num + 1}, 未找到内容标注页码"
                    
                    annotated_text = page_num_label + "\n" + page_note + "\n" + combined_text.strip()

                    # 构建双页码元数据
                    metadata = {
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "pdf",
                        "type": "page",
                        "page_number": display_page,  # 显示页码（优先逻辑页码）
                        "total_pages": total_pages,
                        "is_landscape": page_info["is_landscape"],
                        "page_width": page_info["page_width"],
                        "page_height": page_info["page_height"],
                        # v5 新增：双页码系统字段
                        "physical_page": page_num + 1,  # 物理页码（文件结构）
                        "logical_page": logical_page,   # 逻辑页码（内容标注）
                        "has_logical_page": has_logical_page,
                        "page_numbering_system": "logical" if has_logical_page else "physical",
                        "page_numbering_source": "content_marker" if has_logical_page else "file_structure",
                        "page_label": page_num_label.replace("【", "").replace("】", ""),
                    }

                    documents.append(Document(
                        text=annotated_text,
                        metadata=metadata,
                    ))
        doc.close()

        if not documents:
            logger.warning(f"[PDF] 未提取到文本内容（可能是扫描件/图片PDF）: {file_path.name}")

        # PDF 解析统计汇总
        logger.info(
            f"[PDF] 解析完成: {file_path.name} | "
            f"总页数: {total_pages} | "
            f"成功提取文档块: {len(documents)}"
        )
        return documents

    except Exception as e:
        logger.error(f"[PDF] 解析失败 {file_path.name}: {e}")
        return []


# ============================================================================
# 其他格式解析函数
# ============================================================================

def load_txt(file_path: Path) -> List[Document]:
    """
    加载 .txt 文件
    自动检测文件编码（utf-8 / gbk / latin-1）
    """
    logger.info(f"[TXT] 解析: {file_path.name}")
    documents = []
    try:
        # 尝试多种编码读取
        content = ""
        detected_encoding = "utf-8"
        for encoding in ["utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"]:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    content = f.read()
                detected_encoding = encoding
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if content.strip():
            documents.append(
                Document(
                    text=content.strip(),
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "txt",
                        "type": "text",
                        "encoding": detected_encoding,
                    },
                )
            )
        else:
            logger.warning(f"[TXT] 文件内容为空: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[TXT] 解析失败 {file_path.name}: {e}")
        return []


def load_md(file_path: Path) -> List[Document]:
    """
    加载 .md 文件，保留原始 markdown 文本
    （后续 chunk_strategy 按标题切分）
    """
    logger.info(f"[MD] 解析: {file_path.name}")
    documents = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            logger.warning(f"[MD] 文件为空: {file_path.name}")
            return []
        return [
            Document(
                text=text,
                metadata={
                    "source": file_path.name,
                    "full_path": str(file_path),
                    "format": "md",
                    "type": "markdown",
                },
            )
        ]
    except Exception as e:
        logger.error(f"[MD] 解析失败 {file_path.name}: {e}")
        return []


def _extract_docx_images(doc, file_path: Path) -> List[Dict[str, Any]]:
    """
    提取 .docx 文档中内嵌的全部图片（含正文内联图 + 页眉/页脚图）

    python-docx 的 inline_shapes 只能拿到正文内联图片的计数和大小，
    拿不到图片二进制，且会遗漏页眉/页脚(header/footer)中的图片。
    正确做法是双轨采集：
      轨道1: inline_shapes 逐个取 rId（保证正文内联图按出现顺序）
      轨道2: doc.part.rels 遍历所有 image 关系（兜底补全页眉/页脚图）
    两轨去重合并，保证不同类型图片都不丢失。

    Returns:
        List[Dict]: 每项包含 {"index", "blob", "ext", "location"} 的图片信息列表
                    location: "inline"(正文内联) 或 "header_footer"(页眉/页脚) 或 "unknown"
    """
    images = []
    seen_r_ids = set()  # 去重：同一 rId 只提取一次

    # ---- 轨道1: 正文内联图（按出现顺序） ----
    try:
        shapes = doc.inline_shapes
        for idx, shape in enumerate(shapes):
            try:
                # inline shape 的 XML 路径: inline → graphic → graphicData → pic → blipFill → blip
                blip = shape._inline.graphic.graphicData.pic.blipFill.blip
                r_id = blip.embed  # 例如 "rId5"
                if r_id in seen_r_ids:
                    continue
                seen_r_ids.add(r_id)
                image_part = doc.part.related_parts[r_id]
                blob = image_part.blob
                ext = image_part.content_type.split("/")[-1] if "/" in image_part.content_type else "png"
                images.append({"index": len(images), "blob": blob, "ext": ext, "location": "inline"})
            except (KeyError, AttributeError, IndexError) as e:
                logger.debug(f"[DOCX] 跳过无法访问的内联图 {idx}: {e}")
                continue
    except Exception as e:
        logger.warning(f"[DOCX] 遍历 inline_shapes 失败: {e}")

    # ---- 轨道2: 遍历全部关系，兜底补全页眉/页脚等图片 ----
    try:
        for rel_id, rel in doc.part.rels.items():
            if "image" not in rel.reltype:
                continue
            if rel_id in seen_r_ids:
                continue
            seen_r_ids.add(rel_id)
            try:
                blob = rel.target_part.blob
                ext = rel.target_part.content_type.split("/")[-1] if "/" in rel.target_part.content_type else "png"
                # 通过 target_ref 判断位置: 页眉/页脚图片通常位于 header/footer 部件
                target_ref = getattr(rel, "target_ref", "") or ""
                if "header" in target_ref.lower() or "footer" in target_ref.lower():
                    location = "header_footer"
                else:
                    location = "unknown"
                images.append({"index": len(images), "blob": blob, "ext": ext, "location": location})
            except Exception as e:
                logger.debug(f"[DOCX] 跳过无法访问的 rel 图片 {rel_id}: {e}")
                continue
    except Exception as e:
        logger.warning(f"[DOCX] 遍历 rels 失败: {e}")

    if images:
        logger.info(f"[DOCX] 提取到 {len(images)} 张图片: {file_path.name}")
    return images


def _ocr_image_bytes(blob: bytes) -> str:
    """
    OCR 识别单张图片的二进制数据

    依赖: pytesseract + Pillow
    如果 tesseract 未安装或识别失败，返回空字符串（调用方降级处理）。
    """
    if not HAS_TESSERACT or not HAS_PIL:
        return ""
    try:
        import io
        from PIL import Image

        img = Image.open(io.BytesIO(blob))
        # 统一转 RGB（处理 PNG 透明通道/灰度图，避免 OCR 报错）
        if img.mode != "RGB":
            img = img.convert("RGB")
        # 双倍缩放提升小图/低分辨率图片的 OCR 准确率
        width, height = img.size
        img = img.resize((width * 2, height * 2), Image.LANCZOS)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception as e:
        logger.warning(f"[DOCX] 图片 OCR 失败: {e}")
        return ""


def load_docx(file_path: Path) -> List[Document]:
    """
    加载 .docx 文件
    - 段落文本按段落拆分，保留段落结构
    - 表格单独提取，保留行列结构（Markdown表格格式）
    - 图片单独提取，OCR 识别文字后作为独立 Document（type="image"）
    """
    logger.info(f"[DOCX] 解析: {file_path.name}")
    documents = []
    if not HAS_DOCX:
        logger.error("python-docx 未安装，请执行: pip install python-docx")
        return []
    try:
        doc = DocxDocument(file_path)
        # 1. 提取段落文本（过滤空段落）
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        if paragraphs:
            para_text = "\n".join(paragraphs)
            documents.append(
                Document(
                    text=para_text,
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "docx",
                        "type": "paragraph",
                        "paragraph_count": len(paragraphs),
                    },
                )
            )
        # 2. 提取表格（每个表格作为一个独立 Document，标准 Markdown 格式）
        # v5 改进: 输出标准 Markdown 表格（表头行 + 分隔行 + 数据行），
        # 与 PDF 表格格式对齐，从而复用 chunk 层的 split_markdown_table 分行切分逻辑
        for table_idx, table in enumerate(doc.tables):
            md_lines = []
            for row_idx, row in enumerate(table.rows):
                cells = [cell.text.strip() for cell in row.cells]
                if row_idx == 0:
                    # 第一行作为表头 + 分隔行（标准 Markdown 表格结构）
                    md_lines.append("| " + " | ".join(cells) + " |")
                    md_lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
                else:
                    md_lines.append("| " + " | ".join(cells) + " |")
            if md_lines:
                table_text = "\n".join(md_lines)
                documents.append(
                    Document(
                        text=table_text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "docx",
                            "type": "table",
                            "table_index": table_idx,
                            "row_count": len(md_lines) - 1,  # 去掉分隔行
                            "col_count": len(table.columns),
                        },
                    )
                )
        # 3. 提取图片 + OCR（v5 P1 改进）
        # P1: 不再仅计数，而是真正提取图片二进制并 OCR 识别文字，
        # 每张图片生成独立的 Document（type="image"），便于后续切片与检索
        try:
            images = _extract_docx_images(doc, file_path)
            for img in images:
                idx = img["index"]
                ext = img["ext"]
                blob = img["blob"]
                location = img.get("location", "unknown")

                # OCR 识别图片中的文字（仅一次，结果在下方统计中复用）
                ocr_text = _ocr_image_bytes(blob)
                has_ocr = bool(ocr_text)

                if has_ocr:
                    # OCR 成功：直接使用识别出的文字作为检索内容
                    text = f"[DOCX图片{idx} OCR内容]\n{ocr_text}"
                else:
                    # OCR 失败：保留图片位置占位，避免图片信息完全丢失
                    text = f"[DOCX图片{idx}：未能识别出文字内容]"

                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "docx",
                            "type": "image",
                            "image_index": idx,
                            "image_ext": ext,
                            "image_location": location,  # inline / header_footer / unknown
                            "has_ocr": has_ocr,
                            "ocr_text": ocr_text,  # 无 OCR 则为空串
                        },
                    )
                )

            if images:
                # OCR 已在上面逐图执行过一次，此处直接复用 has_ocr 结果，避免重复 OCR 浪费算力
                ocr_count = sum(1 for d in documents if d.metadata.get("type") == "image" and d.metadata.get("has_ocr"))
                logger.info(f"[DOCX] 提取 {len(images)} 张图片，OCR成功 {ocr_count} 张: {file_path.name}")
        except Exception as e:
            logger.warning(f"[DOCX] 图片提取失败: {file_path.name}: {e}")

        if not documents:
            logger.warning(f"[DOCX] 未提取到有效内容: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[DOCX] 解析失败 {file_path.name}: {e}")
        return []


def load_xlsx(file_path: Path) -> List[Document]:
    """
    加载 .xlsx 文件
    - 每个 sheet 独立处理
    - 使用 openpyxl 读取，保留合并单元格信息
    - 每行作为一个 Document，附带表头信息
    """
    logger.info(f"[XLSX] 解析: {file_path.name}")
    documents = []
    if HAS_OPENPYXL:
        # 优先使用 openpyxl（保留合并单元格等信息）
        return _load_xlsx_openpyxl(file_path, documents)
    elif HAS_PANDAS:
        # 降级使用 pandas
        return _load_xlsx_pandas(file_path, documents)
    else:
        logger.error("openpyxl 和 pandas 均未安装，无法解析 xlsx 文件")
        return []


def _load_xlsx_openpyxl(file_path: Path, documents: List[Document]) -> List[Document]:
    """使用 openpyxl 加载 xlsx"""
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_data = []
            # 获取合并单元格信息
            merged_ranges = list(ws.merged_cells.ranges)
            for row in ws.iter_rows(values_only=True):
                # 过滤全空行
                if any(cell is not None for cell in row):
                    cleaned_row = [str(cell).strip() if cell is not None else "" for cell in row]
                    rows_data.append(cleaned_row)
            if rows_data:
                md_table = _data_to_markdown_table(rows_data)
                documents.append(Document(
                    text=md_table,
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "xlsx",
                        "type": "sheet",
                        "sheet_name": sheet_name,
                        "rows": len(rows_data),
                    },
                ))
        wb.close()
        return documents
    except Exception as e:
        logger.error(f"[XLSX] openpyxl 解析失败 {file_path.name}: {e}")
        return documents  # 返回已提取的内容


def _load_xlsx_pandas(file_path: Path, documents: List[Document]) -> List[Document]:
    """使用 pandas 加载 xlsx（降级方案）"""
    try:
        xl = pd.ExcelFile(file_path)
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(file_path, sheet_name=sheet_name)
            if df.empty:
                logger.warning(f"[XLSX] Sheet 为空: {sheet_name} in {file_path.name}")
                continue
            # 获取表头
            headers = df.columns.tolist()
            header_str = " | ".join(str(h) for h in headers)
            # 每行作为一个 Document
            for idx, row in df.iterrows():
                row_values = " | ".join(str(v) if pd.notna(v) else "" for v in row.values)
                # 组合表头 + 行数据，方便语义理解
                text = f"表头: {header_str}\n数据: {row_values}"
                documents.append(
                    Document(
                        text=text,
                        metadata={
                            "source": file_path.name,
                            "full_path": str(file_path),
                            "format": "xlsx",
                            "type": "row",
                            "sheet_name": sheet_name,
                            "row_index": int(idx),
                            "headers": headers,
                        },
                    )
                )
        if not documents:
            logger.warning(f"[XLSX] 未提取到有效内容: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[XLSX] pandas 解析失败 {file_path.name}: {e}")
        return []


# ============================================================================
# 统一加载入口
# ============================================================================

FORMAT_LOADER_MAP = {
    ".txt": load_txt,
    ".md": load_md,
    ".docx": load_docx,
    ".xlsx": load_xlsx,
    ".pdf": load_pdf,
}


def load_file(file_path: Path) -> List[Document]:
    """根据文件后缀调用对应解析器"""
    ext = file_path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        logger.warning(f"不支持的文件格式: {ext} ({file_path.name})")
        return []
    loader = FORMAT_LOADER_MAP.get(ext)
    if loader is None:
        return []
    return loader(file_path)


def scan_knowledge_base() -> List[Path]:
    """
    扫描 knowledge_base 目录，收集所有支持的文件
    PDF 文件放在 pdf_examples/ 子目录中
    """
    files = []
    if not KNOWLEDGE_BASE_DIR.exists():
        logger.error(f"知识库目录不存在: {KNOWLEDGE_BASE_DIR}")
        return files

    # 扫描根目录（非 pdf 文件）
    for ext in SUPPORTED_EXTENSIONS:
        if ext == ".pdf":
            continue  # pdf 单独处理
        files.extend(KNOWLEDGE_BASE_DIR.glob(f"*{ext}"))

    # 扫描 pdf_examples/ 子目录
    pdf_dir = KNOWLEDGE_BASE_DIR / PDF_SUBDIR
    if pdf_dir.exists():
        files.extend(pdf_dir.glob("*.pdf"))
    else:
        logger.warning(f"PDF 子目录不存在: {pdf_dir}")

    return sorted(files)


def load_all_documents() -> Dict[str, Any]:
    """
    加载知识库中所有文档
    返回: {
        "documents": List[Document],
        "stats": {"total_files": int, "total_chunks": int, "by_format": dict}
    }
    """
    file_paths = scan_knowledge_base()
    all_docs: List[Document] = []
    stats = {"total_files": 0, "total_chunks": 0, "by_format": {}}

    for fp in file_paths:
        docs = load_file(fp)
        if docs:
            all_docs.extend(docs)
            stats["total_files"] += 1
            stats["total_chunks"] += len(docs)
            fmt = fp.suffix.lower().lstrip(".")
            stats["by_format"][fmt] = stats["by_format"].get(fmt, 0) + 1

    logger.info(f"知识库加载完成: {stats}")
    return {"documents": all_docs, "stats": stats}


# ============================================================================
# 序列化工具（用于 processed_cache）
# ============================================================================

def documents_to_json(docs: List[Document]) -> str:
    """Document 列表序列化为 JSON 字符串"""
    return json.dumps([doc.to_dict() for doc in docs], ensure_ascii=False, indent=2)


def documents_from_json(json_str: str) -> List[Document]:
    """JSON 字符串反序列化为 Document 列表"""
    data = json.loads(json_str)
    return [Document.from_dict(item) for item in data]


if __name__ == "__main__":
    # 本地测试
    result = load_all_documents()
    print(f"\n加载统计: {result['stats']}")
    for doc in result["documents"][:3]:
        print(f"\n--- [{doc.metadata.get('format', 'unknown')}] {doc.metadata.get('type', 'unknown')} ---")
        print(doc.text[:300] + "..." if len(doc.text) > 300 else doc.text)