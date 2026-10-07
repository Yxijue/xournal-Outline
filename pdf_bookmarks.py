"""Add nested PDF outlines from PageOutline's UTF-8 manifest. No source overwrites."""
import argparse
import os
from pathlib import Path
import tempfile
from pypdf import PdfReader, PdfWriter
from pypdf.generic import Fit


def add_bookmarks(pdf_path, output=None, overwrite=False):
    source = Path(pdf_path).resolve()
    manifest = Path(str(source) + '.outline.tsv')
    lines = manifest.read_text(encoding='utf-8-sig').splitlines()
    magic, count = lines[0].split('\t')
    if magic != 'PageOutline-v1':
        raise ValueError('无法识别标题数据格式')
    reader = PdfReader(source)
    if reader.is_encrypted:
        raise ValueError('请使用未加密的原始导出 PDF')
    if len(reader.pages) != int(count):
        raise ValueError('PDF 页数与标题数据不一致，请从插件重新导出完整文档')
    parsed = []
    for line in lines[1:]:
        level, page, y, height, title = line.split('\t', 4)
        level, page, y, height = int(level), int(page), float(y), float(height)
        if level not in (1, 2, 3) or not 1 <= page <= len(reader.pages) or height <= 0 or not title:
            raise ValueError('标题数据包含无效的层级、页码或标题')
        parsed.append((level, page, y, height, title))
    if not parsed:
        raise ValueError('没有可写入的标题')
    target = Path(output).resolve() if output else source.with_name(source.stem + '-带书签.pdf')
    if target == source:
        raise ValueError('不能覆盖原始输入 PDF')
    existed = target.exists()
    if existed and not overwrite:
        raise ValueError('目标已存在，尚未确认覆盖')
    previous = target.stat() if existed else None
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    parents = {}
    for level, page, y, height, title in parsed:
        # Missing intermediate levels attach to the nearest available ancestor.
        parent = next((parents[k] for k in range(level-1, 0, -1) if k in parents), None)
        crop = reader.pages[page-1].cropbox
        top = float(crop.top) - max(0, min(1, y/height))*float(crop.height)
        ref = writer.add_outline_item(title, page-1, parent=parent, fit=Fit.fit_horizontally(top))
        parents = {k: v for k, v in parents.items() if k < level}
        parents[level] = ref
    writer.page_mode = '/UseOutlines'
    fd, temp = tempfile.mkstemp(prefix='.bookmarks-', suffix='.pdf', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            writer.write(stream)
        # Read back destinations before delivering.
        verify = PdfReader(temp)
        def flatten(items):
            for item in items:
                if isinstance(item, list):
                    yield from flatten(item)
                else:
                    yield item
        actual = list(flatten(verify.outline))
        if len(actual) < len(parsed):
            raise ValueError('输出书签数量不足')
        # Existing background PDF outlines may also be present.
        for expected, item in zip(parsed, actual[-len(parsed):]):
            if item.title != expected[4] or verify.get_destination_page_number(item) != expected[1]-1:
                raise ValueError('书签回读校验失败')
        if len(verify.pages) != int(count):
            raise ValueError('输出页数校验失败')
        # Validate completely before atomic installation in the same directory.
        if existed:
            now = target.stat()
            if (now.st_size, now.st_mtime_ns, now.st_ino) != (previous.st_size, previous.st_mtime_ns, previous.st_ino):
                raise ValueError('原 PDF 在导出期间已变化，请重新导出')
            os.replace(temp, target)
        else:
            # Atomic no-clobber operation: if another file appeared, leave it intact.
            os.link(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)
    return target


def confirm_overwrite(target):
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk(); root.withdraw()
    try:
        return messagebox.askyesno('覆盖已有 PDF？',
            f'文件已存在：\n{target}\n\n确认覆盖？新 PDF 验证成功后才会替换原文件。', parent=root, default='no')
    finally:
        root.destroy()


def run_job(job, confirm=confirm_overwrite):
    job=Path(job)
    source,target=job.read_text(encoding='utf-8').splitlines()
    overwrite=Path(target).exists()
    if overwrite and not confirm(target):
        job.with_name(job.name+'.result').write_text('CANCEL\n',encoding='utf-8')
        return
    result=add_bookmarks(source,target,overwrite=overwrite)
    job.with_name(job.name+'.result').write_text('OK\n'+str(result),encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pdf', nargs='?')
    parser.add_argument('--output')
    parser.add_argument('--job')
    parser.add_argument('--job-base64')
    args = parser.parse_args()
    if args.job_base64:
        import base64
        args.job=base64.b64decode(args.job_base64).decode('utf-8')
    if args.job:
        job=Path(args.job)
        try:
            run_job(job)
        except Exception as error:
            job.with_name(job.name+'.result').write_text('ERROR\n'+str(error),encoding='utf-8')
            raise SystemExit(1)
        return
    if args.pdf:
        print(add_bookmarks(args.pdf, args.output))
        return
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    try:
        path = filedialog.askopenfilename(title='选择插件导出的 PDF（旁边应有 .outline.tsv 文件）', filetypes=[('PDF', '*.pdf')])
        if not path:
            return
        suggested = Path(path).stem + '-带书签.pdf'
        output = filedialog.asksaveasfilename(title='保存带书签的 PDF', initialdir=str(Path(path).parent), initialfile=suggested, defaultextension='.pdf', filetypes=[('PDF', '*.pdf')])
        if output:
            overwrite=Path(output).exists()
            if overwrite and not messagebox.askyesno('覆盖已有 PDF？','新 PDF 验证成功后才会替换原文件，是否继续？',default='no'):
                return
            result = add_bookmarks(path, output, overwrite=overwrite)
            messagebox.showinfo('完成', f'已生成真正的多级 PDF 书签：\n{result}\n可在 Acrobat 或 Xournal++ 的左侧大纲中跳转。')
    except Exception as error:
        messagebox.showerror('生成失败', str(error))
    finally:
        root.destroy()


if __name__ == '__main__':
    main()
