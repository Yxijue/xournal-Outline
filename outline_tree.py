"""Independent Windows outline tree. All document operations run inside Lua."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile
import time
import json
import hashlib
import copy
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from urllib.parse import quote
from navigation import NavigationStore, note_key
from portable_paths import DATA_ROOT, INSTALL_ROOT, encode_path
from rounded_theme import install_round_borders

ROOT = DATA_ROOT / 'cache'
STATE = ROOT / 'PageOutline-tree.tsv'
COMMAND = ROOT / 'PageOutline-command.tsv'
PREFERENCES = DATA_ROOT / 'tree-preferences.json'


def claim_instance():
    """Keep one window per installation without a PowerShell process scan."""
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    identity=hashlib.sha256(os.path.normcase(str(INSTALL_ROOT)).encode('utf-8')).hexdigest()[:24]
    handle=kernel.CreateMutexW(None,False,'Local\\PageOutline-'+identity)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error()!=183:return handle
    kernel.CloseHandle(handle)
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.FindWindowW.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR]
    user.FindWindowW.restype=wintypes.HWND
    user.ShowWindow.argtypes=[wintypes.HWND,ctypes.c_int]
    user.SetForegroundWindow.argtypes=[wintypes.HWND]
    window=user.FindWindowW(None,'独立大纲 - PageOutline')
    if window:
        user.ShowWindow(window,9);user.SetForegroundWindow(window)
    return None


def load_preferences(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data,dict) and isinstance(data.get('documents',{}),dict):
            data['documents']={encode_path(key) if Path(key).is_absolute() else key:value for key,value in data.get('documents',{}).items()}
        return data if isinstance(data,dict) and isinstance(data.get('documents',{}),dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.tree-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False)
        os.replace(temp,path)
    finally:
        Path(temp).unlink(missing_ok=True)


def stable_nodes(rows):
    """Title ancestry + duplicate occurrence, independent of page and row number."""
    parents={}; counts={}
    for index,level,page,title in rows:
        parent=next((parents[k] for k in range(level-1,0,-1) if k in parents),'')
        token=(parent,level,title)
        counts[token]=counts.get(token,0)+1
        key=hashlib.sha256(json.dumps([*token,counts[token]],ensure_ascii=False).encode('utf-8')).hexdigest()
        yield key,parent,index,page,title
        parents={k:v for k,v in parents.items() if k<level};parents[level]=key


def filtered_nodes(nodes,query):
    query=query.strip().casefold()
    if not query: return nodes
    by_key={row[0]:row for row in nodes}; included=set()
    for key,parent,index,page,title in nodes:
        if query in title.casefold():
            while key:
                included.add(key);key=by_key[key][1]
    return [row for row in nodes if row[0] in included]


def read_state(path=None):
    path=path or STATE
    lines = path.read_text(encoding='utf-8').splitlines()
    header=lines[0].split('\t')
    magic,session,epoch=header[:3]
    if magic not in ('PageOutlineTree-v1','PageOutlineTree-v2'):
        raise ValueError('大纲数据格式不正确')
    status, document = lines[1].split('\t', 1)
    rows = []
    for index, line in enumerate(lines[2:], 1):
        level, page, title = line.split('\t', 2)
        level, page = int(level), int(page)
        if level not in (1, 2, 3) or page < 1:
            raise ValueError('大纲层级或页码无效')
        rows.append((str(index), level, page, title))
    current=int(header[3]) if len(header)>3 else 1
    count=int(header[4]) if len(header)>4 else max([row[2] for row in rows] or [1])
    return session, int(epoch), status, document, rows,current,count


def hierarchy(rows):
    parents = {}
    for key, level, page, title in rows:
        parent = next((parents[k] for k in range(level-1, 0, -1) if k in parents), '')
        yield key, parent, page, title
        parents = {k:v for k,v in parents.items() if k < level}
        parents[level] = key


def windows():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def callback(hwnd, _):
        n = user.GetWindowTextLengthW(hwnd)
        text = ctypes.create_unicode_buffer(n+1)
        user.GetWindowTextW(hwnd, text, n+1)
        if user.IsWindowVisible(hwnd) and 'xournal++' in text.value.lower():
            found.append((hwnd, text.value))
        return True
    user.EnumWindows(callback, 0)
    return found


def signal_window(hwnd):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.GetForegroundWindow.restype = wintypes.HWND
    if not user.IsWindow(hwnd):
        raise ValueError('Xournal++ 窗口已关闭，请重新选择窗口')
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)
    if user.GetForegroundWindow()!=hwnd:
        user.SetForegroundWindow(hwnd)
        time.sleep(.10)
    if user.GetForegroundWindow() != hwnd:
        raise ValueError('无法切换到 Xournal++。请手动激活它后再点击；也可按 Ctrl+Alt+Shift+F12 执行待处理请求。')
    # Correct INPUT union size on both 32- and 64-bit Windows.
    pointer = ctypes.c_size_t
    class Keyboard(ctypes.Structure):
        _fields_ = [('vk',wintypes.WORD),('scan',wintypes.WORD),('flags',wintypes.DWORD),('time',wintypes.DWORD),('extra',pointer)]
    class Mouse(ctypes.Structure):
        _fields_ = [('dx',wintypes.LONG),('dy',wintypes.LONG),('data',wintypes.DWORD),('flags',wintypes.DWORD),('time',wintypes.DWORD),('extra',pointer)]
    class Union(ctypes.Union):
        _fields_ = [('keyboard',Keyboard),('mouse',Mouse)]
    class Input(ctypes.Structure):
        _fields_ = [('type',wintypes.DWORD),('data',Union)]
    keys = [0x11,0x12,0x10,0x7B]  # Ctrl, Alt, Shift, F12
    events=[]
    for vk in keys:
        events.append(Input(1,Union(keyboard=Keyboard(vk,0,0,0,0))))
    for vk in reversed(keys):
        events.append(Input(1,Union(keyboard=Keyboard(vk,0,2,0,0))))
    array=(Input*len(events))(*events)
    user.SendInput.argtypes=[wintypes.UINT,ctypes.POINTER(Input),ctypes.c_int]
    if user.SendInput(len(events),array,ctypes.sizeof(Input)) != len(events):
        raise ValueError('快捷键发送失败。请让 Xournal++ 和大纲工具以相同权限运行。')


class OutlineApp:
    def apply_theme(self):
        dark=self.theme.get()!='浅色'
        self.colors={'bg':'#1b1e23','panel':'#242931','text':'#d6dbe2','muted':'#98a3b1','border':'#39424f',
            'hover':'#343e4b','selected':'#354b5d','accent':'#95b4d0'} if dark else {
            'bg':'#f1f3f6','panel':'#ffffff','text':'#27313e','muted':'#657184','border':'#d2d9e2',
            'hover':'#e1e7ef','selected':'#d5e2ef','accent':'#466e96'}
        c=self.colors;style=ttk.Style(self.root);style.theme_use('clam')
        font=('Microsoft YaHei UI',10)
        self.root.configure(background=c['bg'])
        self.root.option_add('*Menu.font',font);self.root.option_add('*Listbox.font',font)
        self.root.option_add('*Background',c['bg']);self.root.option_add('*Foreground',c['text'])
        self.root.option_add('*Listbox.background',c['panel']);self.root.option_add('*Listbox.foreground',c['text'])
        self.root.option_add('*Listbox.selectBackground',c['selected']);self.root.option_add('*Listbox.selectForeground',c['text'])
        style.configure('.',font=font,background=c['bg'],foreground=c['text'],bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'])
        style.configure('TFrame',background=c['bg'])
        style.configure('TLabel',background=c['bg'],foreground=c['text'])
        style.configure('Title.TLabel',font=('Microsoft YaHei UI',14,'bold'))
        style.configure('Muted.TLabel',foreground=c['muted'],font=('Microsoft YaHei UI',9))
        style.configure('TButton',padding=(7,5),width=8,background=c['panel'],relief='flat')
        style.map('TButton',background=[('pressed',c['selected']),('active',c['hover'])],foreground=[('disabled',c['muted'])])
        style.configure('TCheckbutton',background=c['bg'])
        style.map('TCheckbutton',background=[('active',c['bg'])],indicatorcolor=[('selected',c['selected']),('!selected',c['panel'])])
        for name in ('TEntry','TCombobox'):
            style.configure(name,fieldbackground=c['panel'],background=c['panel'],foreground=c['text'],insertcolor=c['text'],padding=5,arrowcolor=c['text'])
            style.map(name,fieldbackground=[('readonly',c['panel']),('disabled',c['bg'])],foreground=[('disabled',c['muted']),('readonly',c['text'])],selectbackground=[('!disabled',c['selected'])],selectforeground=[('!disabled',c['text'])])
        style.configure('TNotebook',background=c['bg'],borderwidth=0,bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'])
        style.configure('TNotebook.Tab',background=c['bg'],foreground=c['muted'],padding=(11,6),bordercolor=c['border'],lightcolor=c['border'],darkcolor=c['border'],focuscolor=c['bg'])
        style.map('TNotebook.Tab',
            padding=[('selected',(17,9)),('!selected',(11,6))],
            expand=[('selected',(0,0,0,0)),('!selected',(0,0,0,0))],
            font=[('selected',('Microsoft YaHei UI',11,'bold')),('!selected',('Microsoft YaHei UI',10))],
            background=[('selected',c['selected']),('active',c['hover'])],foreground=[('selected',c['text'])])
        style.configure('Module.TNotebook.Tab',padding=(14,7),font=('Microsoft YaHei UI',10))
        style.map('Module.TNotebook.Tab',
            padding=[('selected',(22,10)),('!selected',(14,7))],
            expand=[('selected',(0,0,0,0)),('!selected',(0,0,0,0))],
            font=[('selected',('Microsoft YaHei UI',11,'bold')),('!selected',('Microsoft YaHei UI',10))],
            background=[('selected',c['selected']),('active',c['hover']),('!selected',c['bg'])],
            foreground=[('selected',c['text']),('!selected',c['muted'])])
        style.configure('Treeview',background=c['panel'],fieldbackground=c['panel'],foreground=c['text'],rowheight=29,borderwidth=0,font=font)
        style.map('Treeview',background=[('selected',c['selected'])],foreground=[('selected',c['text'])])
        style.configure('Treeview.Heading',background=c['bg'],foreground=c['muted'],font=('Microsoft YaHei UI',9),padding=(5,7),relief='flat')
        style.map('Treeview.Heading',background=[('active',c['hover'])])
        style.configure('Vertical.TScrollbar',background=c['border'],troughcolor=c['panel'],arrowcolor=c['muted'],borderwidth=0)
        self.rounded_images=install_round_borders(self.root,style,c)
        self.icons_by_theme=getattr(self,'icons_by_theme',{})
        self.icons=self.icons_by_theme.setdefault(dark,{})
        def icon(kind):
            pic=tk.PhotoImage(master=self.root,width=20,height=16)
            if kind in ('分类','库'):
                pic.put('#b7a075' if dark else '#a48345',to=(1,5,15,14));pic.put('#b7a075' if dark else '#a48345',to=(2,3,8,6))
                pic.put(c['panel'],to=(2,7,14,13))
            elif kind in ('笔记','书签'):
                pic.put(c['accent'],to=(3,2,13,15));pic.put(c['panel'],to=(4,3,12,14))
                for y in (6,9,12):pic.put(c['accent'],to=(6,y,10,y+1))
            else:pic.put(c['muted'],to=(6,6,10,10))
            return pic
        if not self.icons:
            for kind in ('分类','库','笔记','书签','标题'):self.icons[kind]=icon(kind)
        if hasattr(self,'menu'):self.menu.configure(background=c['panel'],foreground=c['text'],activebackground=c['selected'],activeforeground=c['text'],borderwidth=0)

    def change_theme(self):
        self.apply_theme()
        self.render_library();self.render_bookmarks();self.render_recent();self.schedule_save()

    def __init__(self, root, preferences=PREFERENCES, install_root=INSTALL_ROOT, background_sync=True):
        self.root=root
        self.background_sync=background_sync
        self.library_dirty=True;self.library_search_timer=None;self.library_render_signature=None
        self.library_results={};self.library_source_signature=None
        self.preferences_path=preferences
        self.preferences=load_preferences(preferences)
        self.navigation=NavigationStore(preferences.parent/'navigation.json',install_root)
        self.library_revision=0;self.sync_future=None;self.sync_executor=None
        self.sync_requested=True;self.sync_force=False;self.sync_next=0;self.sync_closed=False
        if background_sync:self.navigation._folder_cache=[]
        self.current_page=1;self.current_document='';self.bookmark_pending=False
        self.navigation_job=None
        self.rows=[];self.nodes=[];self.indices={};self.doc_key=None;self.state_stat=None
        self.save_timer=None;self.search_timer=None
        root.title('独立大纲 - PageOutline')
        try: root.geometry(self.preferences.get('geometry','440x760'))
        except (tk.TclError,TypeError): root.geometry('440x760')
        root.minsize(380,560)
        self.theme=tk.StringVar(value=self.preferences.get('theme','深色'))
        self.apply_theme()
        self.session=''; self.epoch=0; self.pending=False; self.signature=None
        self.status=tk.StringVar(value='先在插件菜单选择“打开/刷新独立大纲窗口”')
        self.document=tk.StringVar(value='当前笔记')
        self.directory=tk.StringVar(value='当前文件所在目录')
        toolbar=ttk.Frame(root,padding=(12,10)); toolbar.pack(fill='x')
        options=ttk.Frame(toolbar);options.pack(fill='x')
        self.topmost=tk.BooleanVar(value=bool(self.preferences.get('topmost',False)))
        root.attributes('-topmost',self.topmost.get())
        ttk.Checkbutton(options,text='置顶',variable=self.topmost,command=self.toggle_topmost).pack(side='left')
        theme=ttk.Combobox(options,textvariable=self.theme,values=('深色','浅色'),state='readonly',width=5)
        theme.pack(side='right');theme.bind('<<ComboboxSelected>>',lambda _:self.change_theme())
        ttk.Label(toolbar,textvariable=self.document,style='Title.TLabel').pack(fill='x',pady=(8,2))
        ttk.Label(toolbar,textvariable=self.directory,style='Muted.TLabel',wraplength=350).pack(fill='x',pady=(0,8))
        controls=ttk.Frame(toolbar); controls.pack(fill='x')
        ttk.Button(controls,text='选择窗口',width=8,command=self.find_windows).pack(side='left')
        ttk.Button(controls,text='刷新',width=6,command=lambda:self.request('refresh',0)).pack(side='left',padx=5)
        ttk.Button(controls,text='返回',width=6,command=self.go_back).pack(side='right')
        self.combo=ttk.Combobox(toolbar,state='readonly')
        self.modules=ttk.Notebook(root,style='Module.TNotebook');self.modules.pack(fill='both',expand=True,padx=12)
        current=ttk.Frame(self.modules,padding=(8,10));self.modules.add(current,text='当前笔记')
        library_module=ttk.Frame(self.modules,padding=(8,10));self.modules.add(library_module,text='笔记库')
        current_controls=ttk.Frame(current);current_controls.pack(fill='x',pady=(0,6))
        ttk.Button(current_controls,text='展开',width=7,command=lambda:self.expand(True)).pack(side='left')
        ttk.Button(current_controls,text='折叠',width=7,command=lambda:self.expand(False)).pack(side='left',padx=5)
        self.search=tk.StringVar()
        searchbar=ttk.Frame(current);searchbar.pack(fill='x',pady=(0,6))
        ttk.Label(searchbar,text='搜索标题').pack(side='left')
        self.search_entry=ttk.Entry(searchbar,textvariable=self.search);self.search_entry.pack(side='left',fill='x',expand=True,padx=4)
        ttk.Button(searchbar,text='清除',width=5,command=lambda:self.search.set('')).pack(side='right')
        self.search.trace_add('write',self.schedule_search)
        levels=ttk.Frame(current);levels.pack(fill='x',pady=(0,10))
        self.level_buttons=[]
        ttk.Label(levels,text='标题层级').pack(side='left',padx=(0,6))
        for level in (1,2,3):
            button=ttk.Button(levels,text=f'{level}级',width=5,command=lambda n=level:self.set_level(n));button.pack(side='left',padx=2);self.level_buttons.append(button)
        root.bind('<Control-f>',lambda _:self.focus_search())
        self.search_entry.bind('<Escape>',lambda _:self.search.set(''))
        self.tabs=ttk.Notebook(current);self.tabs.pack(fill='both',expand=True)
        self.library_tabs=ttk.Notebook(library_module);self.library_tabs.pack(fill='both',expand=True)
        frame=ttk.Frame(self.tabs);self.tabs.add(frame,text='大纲')
        self.tree=ttk.Treeview(frame,columns=('page',),show='tree headings',selectmode='browse')
        self.tree.heading('#0',text='标题'); self.tree.heading('page',text='页码')
        self.tree.column('#0',width=275); self.tree.column('page',width=55,stretch=False,anchor='center')
        bar=ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side='left',fill='both',expand=True); bar.pack(side='right',fill='y')
        self.tree.bind('<ButtonRelease-1>',self.click)
        self.tree.bind('<Return>',lambda _:self.jump_selection())
        self.tree.bind('<Button-3>',self.level_menu)
        self.menu=tk.Menu(root,tearoff=False)
        self.menu.configure(background=self.colors['panel'],foreground=self.colors['text'],activebackground=self.colors['selected'],activeforeground=self.colors['text'])
        for level in (1,2,3):
            self.menu.add_command(label=f'设为{level}级标题',command=lambda n=level:self.set_level(n))
        self.create_navigation_tabs()
        self.tabs.bind('<<NotebookTabChanged>>',self.tab_changed)
        self.modules.bind('<<NotebookTabChanged>>',self.tab_changed)
        self.library_tabs.bind('<<NotebookTabChanged>>',self.tab_changed)
        for event in ('<<TreeviewOpen>>','<<TreeviewClose>>','<<TreeviewSelect>>'):
            self.tree.bind(event,lambda _:self.schedule_save(),add='+')
        root.bind('<Configure>',lambda event:self.schedule_save() if event.widget is root else None,add='+')
        root.protocol('WM_DELETE_WINDOW',self.close)
        ttk.Label(root,textvariable=self.status,style='Muted.TLabel',wraplength=365,padding=(12,10)).pack(side='bottom',fill='x',before=self.modules)
        self.find_windows(); self.poll()
        if background_sync:
            self.sync_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='notes-index')
            root.bind('<FocusIn>',lambda event:self.sync_library() if event.widget is root and self.library_visible() else None,add='+')
            root.bind('<FocusIn>',lambda event:self.check_navigation() if event.widget is root else None,add='+')
            root.after(100,self.check_library_sync)

    def tab_changed(self,_=None):
        # Hidden current-note controls need no state changes when switching
        # the outer module. Their enabled state depends only on the inner tab.
        state='normal' if self.tabs.index(self.tabs.select())==0 else 'disabled'
        if getattr(self,'controls_state',None)!=state:
            self.controls_state=state
            self.search_entry.configure(state=state)
            for button in self.level_buttons:button.configure(state=state)
        if self.library_visible():self.sync_library()
        if self.library_visible() and self.library_dirty:self.render_library()

    def library_visible(self):
        return self.modules.index(self.modules.select())==1 and self.library_tabs.index(self.library_tabs.select())==0

    def focus_search(self):
        if self.modules.index(self.modules.select())==1:
            self.library_tabs.select(0);self.library_search_entry.focus_set()
        else:
            self.tabs.select(0);self.tab_changed();self.search_entry.focus_set()

    def sync_library(self,force=False):
        self.sync_requested=True;self.sync_force=self.sync_force or force
        # Rapid tab/focus changes reuse the ongoing or recent disk scan.
        # Explicit "sync folders" still bypasses the three-second interval.
        if force:self.sync_next=0

    def check_library_sync(self):
        if self.sync_closed:return
        if self.sync_future and self.sync_future.done():
            future=self.sync_future;self.sync_future=None
            try:
                result=future.result()
                if self.sync_revision==self.library_revision:
                    if self.navigation.apply_disk_scan(result):self.render_library()
                    if self.navigation.last_retargets:
                        self.retarget_preferences(self.navigation.last_retargets);self.render_bookmarks();self.render_recent()
                    if result['errors']:self.status.set('部分笔记暂无法索引，稍后自动重试：'+result['errors'][0])
                else:self.sync_requested=True
            except Exception as error:self.status.set('笔记库同步失败：'+str(error))
            self.sync_next=time.monotonic()+3
        visible=self.library_visible() and self.root.state()!='iconic'
        if not self.sync_future and self.sync_executor and time.monotonic()>=self.sync_next and (self.sync_requested or visible):
            snapshot=copy.copy(self.navigation);snapshot.data=copy.deepcopy(self.navigation.data)
            self.sync_revision=self.library_revision;force=self.sync_force
            self.sync_requested=False;self.sync_force=False
            self.sync_future=self.sync_executor.submit(snapshot.scan_disk,force)
        self.root.after(200,self.check_library_sync)

    def create_navigation_tabs(self):
        bookmarks=ttk.Frame(self.tabs);self.tabs.add(bookmarks,text='书签')
        bar=ttk.Frame(bookmarks);bar.pack(fill='x',pady=5)
        ttk.Button(bar,text='收藏当前页',command=self.add_bookmark).pack(side='left')
        ttk.Button(bar,text='改名',command=self.rename_bookmark).pack(side='left')
        ttk.Button(bar,text='删除',command=self.delete_bookmark).pack(side='left')
        self.bookmark_tag=tk.StringVar(value='普通')
        ttk.Combobox(bookmarks,textvariable=self.bookmark_tag,values=['普通','待复习','重要例题','上次阅读'],state='readonly').pack(fill='x')
        self.bookmark_tree=self.navigation_tree(bookmarks,('note','page','tag'),('笔记','页','分类'))
        self.bookmark_tree.bind('<Double-1>',lambda _:self.open_bookmark())
        self.bookmark_tree.bind('<Return>',lambda _:self.open_bookmark())
        ttk.Button(bookmarks,text='打开选中书签',command=self.open_bookmark).pack(fill='x')
        library=ttk.Frame(self.library_tabs);self.library_tabs.add(library,text='分类库')
        controls=ttk.Frame(library);controls.pack(fill='x',pady=5)
        ttk.Button(controls,text='导入笔记',command=self.add_notes).pack(side='left')
        ttk.Button(controls,text='同步文件夹',command=self.reindex_notes).pack(side='left')
        ttk.Button(controls,text='移出库',command=self.remove_note).pack(side='left')
        categories=ttk.Frame(library);categories.pack(fill='x')
        ttk.Button(categories,text='新建分类',command=self.add_category).pack(side='left')
        ttk.Button(categories,text='移动到分类',command=self.move_to_category).pack(side='left')
        expansion=ttk.Frame(library);expansion.pack(fill='x')
        ttk.Button(expansion,text='展开全部',command=lambda:self.expand_library(True)).pack(side='left')
        ttk.Button(expansion,text='折叠全部',command=lambda:self.expand_library(False)).pack(side='left')
        self.library_query=tk.StringVar()
        self.library_search_entry=ttk.Entry(library,textvariable=self.library_query);self.library_search_entry.pack(fill='x',pady=4)
        self.library_query.trace_add('write',self.schedule_library_search)
        self.library_tree=self.navigation_tree(library,('page',),('页',))
        self.library_tree.bind('<Double-1>',lambda _:self.open_library())
        self.library_tree.bind('<Return>',lambda _:self.open_library())
        self.library_tree.bind('<Button-3>',self.library_context)
        self.library_tree.bind('<ButtonPress-1>',self.drag_start,add='+')
        self.library_tree.bind('<B1-Motion>',self.drag_motion,add='+')
        self.library_tree.bind('<ButtonRelease-1>',self.drag_release,add='+')
        ttk.Button(library,text='打开选中结果',command=self.open_library).pack(fill='x')
        recent=ttk.Frame(self.library_tabs);self.library_tabs.add(recent,text='最近阅读')
        self.recent_tree=self.navigation_tree(recent,('page',),('页',))
        self.recent_tree.bind('<Double-1>',lambda _:self.open_recent())
        self.recent_tree.bind('<Return>',lambda _:self.open_recent())
        ttk.Button(recent,text='打开上次阅读位置',command=self.open_recent).pack(fill='x')
        self.render_bookmarks();self.render_library();self.render_recent()

    def render_recent(self):
        signature=(self.theme.get(),tuple((r['path'],r['page']) for r in self.navigation.data.get('recent',[])))
        if signature==getattr(self,'recent_render_signature',None):return
        self.recent_render_signature=signature
        self.recent_tree.delete(*self.recent_tree.get_children())
        for i,record in enumerate(self.navigation.data.get('recent',[])):
            self.recent_tree.insert('','end',iid=str(i),text=Path(record['path']).stem,values=(record['page'],),image=self.icons['笔记'])

    def open_recent(self):
        selected=self.recent_tree.selection()
        if selected:
            record=self.navigation.data['recent'][int(selected[0])];self.open_note(record['path'],record['page'])

    def library_context(self,event):
        key=self.library_tree.identify_row(event.y)
        if not key:return
        self.library_tree.selection_set(key);item=self.library_results[key]
        menu=tk.Menu(self.root,tearoff=False,background=self.colors['panel'],foreground=self.colors['text'],activebackground=self.colors['selected'],activeforeground=self.colors['text'])
        menu.add_command(label='打开所在文件夹',command=self.show_folder)
        if item['kind'] in ('分类','笔记','标题'):menu.add_command(label='重命名',command=self.rename_library_item)
        if item['kind']=='分类':menu.add_command(label='移除分类（保留文件）',command=self.archive_library_category)
        if item['kind'] in ('笔记','标题'):menu.add_command(label='移动到分类',command=self.move_to_category)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()

    def selected_library_item(self):
        selected=self.library_tree.selection()
        return self.library_results.get(selected[0],{}) if selected else {}

    def show_folder(self):
        item=self.selected_library_item()
        folder=self.navigation.category_directory(item.get('category','')) if item.get('kind') in ('库','分类') else Path(item['path']).parent
        folder.mkdir(parents=True,exist_ok=True);os.startfile(folder)

    def allow_relocation(self,path):
        current=getattr(self,'current_document','')
        if current and (note_key(current)==note_key(path) or Path(current).resolve().is_relative_to(Path(path).resolve())):
            messagebox.showinfo('先切换笔记','请先保存当前笔记，并在 Xournal++ 中切换到其他笔记，再操作文件或分类。');return False
        return True

    def finish_relocation(self,changes):
        self.library_revision+=1;self.retarget_preferences(changes)
        self.render_library();self.render_bookmarks();self.render_recent()
        self.status.set('文件位置与导航记录已同步更新。')

    def rename_library_item(self):
        item=self.selected_library_item()
        if item.get('kind') not in ('分类','笔记','标题'):return
        category=item['kind']=='分类';path=self.navigation.category_directory(item['category']) if category else Path(item['path'])
        if not self.allow_relocation(path):return
        name=simpledialog.askstring('重命名','新名称：',initialvalue=path.name if category else path.stem,parent=self.root)
        if name:
            try:self.finish_relocation(self.navigation.rename_category(item['category'],name) if category else self.navigation.rename_note(path,name))
            except (OSError,ValueError) as error:messagebox.showerror('重命名失败',str(error))

    def archive_library_category(self):
        item=self.selected_library_item()
        if item.get('kind')!='分类':return
        if not self.allow_relocation(self.navigation.category_directory(item['category'])):return
        if messagebox.askyesno('移除分类？','空分类将删除；有内容的分类将整体移入“未分类”，保留所有文件和子分类。',parent=self.root):
            try:self.finish_relocation(self.navigation.archive_category(item['category']))
            except (OSError,ValueError) as error:messagebox.showerror('操作失败',str(error))

    def drag_start(self,event):
        key=self.library_tree.identify_row(event.y)
        self.drag_item=self.library_results.get(key,{})
        self.drag_xy=(event.x,event.y);self.dragging=False

    def drag_motion(self,event):
        if getattr(self,'drag_item',{}).get('kind') in ('笔记','标题') and abs(event.x-self.drag_xy[0])+abs(event.y-self.drag_xy[1])>8:
            self.dragging=True;self.library_tree.configure(cursor='hand2')

    def drag_release(self,event):
        self.library_tree.configure(cursor='')
        if not getattr(self,'dragging',False):return
        self.dragging=False;target=self.library_results.get(self.library_tree.identify_row(event.y),{})
        if target.get('kind') not in ('库','分类'):return
        source=self.drag_item['path'];category=target.get('category') or '未分类'
        if not self.allow_relocation(source):return
        try:self.finish_relocation(self.navigation.move_note(source,category))
        except (OSError,ValueError) as error:messagebox.showerror('移动失败',str(error))

    def navigation_tree(self,parent,columns,labels):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=columns,show='tree headings',selectmode='browse')
        tree.heading('#0',text='名称 / 标题');tree.column('#0',width=240,minwidth=130,stretch=True)
        for col,label in zip(columns,labels):
            tree.heading(col,text=label);tree.column(col,width=44 if col=='page' else 70,minwidth=36,stretch=False,anchor='center' if col=='page' else 'w')
        scroll=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
        tree.configure(yscrollcommand=scroll.set);tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        return tree

    def render_bookmarks(self):
        signature=(self.theme.get(),tuple(tuple(sorted(mark.items())) for mark in self.navigation.data['bookmarks']))
        if signature==getattr(self,'bookmark_render_signature',None):return
        self.bookmark_render_signature=signature
        selected=self.bookmark_tree.selection()
        self.bookmark_tree.delete(*self.bookmark_tree.get_children())
        for mark in self.navigation.data['bookmarks']:
            self.bookmark_tree.insert('','end',iid=mark['id'],text=mark['title'],values=(Path(mark['path']).stem,mark['page'],mark['tag']),image=self.icons['书签'])
        if selected and self.bookmark_tree.exists(selected[0]):self.bookmark_tree.selection_set(selected[0])

    def add_bookmark(self):
        if self.pending:return
        self.bookmark_pending=True
        if not self.request('info',0):self.bookmark_pending=False

    def finish_bookmark(self):
        self.bookmark_pending=False
        path=self.current_document
        if not path.lower().endswith('.xopp') or not Path(path).is_file():
            messagebox.showinfo('先保存笔记','请先将当前笔记保存为 .xopp，然后刷新大纲。');return
        title=simpledialog.askstring('收藏当前页',f'第 {self.current_page} 页的书签名称：',initialvalue=f'第{self.current_page}页',parent=self.root)
        if title and title.strip():
            try:
                self.navigation.bookmark(path,self.current_page,title,self.bookmark_tag.get())
                self.render_bookmarks();self.status.set('书签已保存，不改变大纲层级。')
            except (OSError,ValueError) as error:messagebox.showerror('无法保存',str(error))

    def selected_bookmark(self):
        selected=self.bookmark_tree.selection()
        return next((m for m in self.navigation.data['bookmarks'] if selected and m['id']==selected[0]),None)

    def rename_bookmark(self):
        mark=self.selected_bookmark()
        if not mark:return
        title=simpledialog.askstring('重命名书签','新名称：',initialvalue=mark['title'],parent=self.root)
        if title and title.strip():mark['title']=title.strip();self.navigation.save();self.render_bookmarks()

    def delete_bookmark(self):
        mark=self.selected_bookmark()
        if mark and messagebox.askyesno('删除书签？',mark['title'],parent=self.root):
            self.navigation.data['bookmarks'].remove(mark);self.navigation.save();self.render_bookmarks()

    def open_bookmark(self):
        mark=self.selected_bookmark()
        if mark:self.open_note(mark['path'],mark['page'])

    def add_notes(self):
        category=self.selected_category() or '未分类'
        paths=filedialog.askopenfilenames(title=f'导入已保存的笔记到 {category}（保留原文件）',filetypes=[('Xournal++ 笔记','*.xopp')])
        if paths:
            try:
                changes=self.navigation.import_notes(paths,category);self.retarget_preferences(changes)
                self.library_revision+=1
                self.render_library();self.render_bookmarks();self.status.set('已导入副本，后续从笔记库打开副本编辑。')
            except (OSError,ValueError) as error:messagebox.showerror('索引失败',str(error))

    def selected_category(self):
        selected=self.library_tree.selection();key=selected[0] if selected else ''
        while key:
            item=self.library_results.get(key,{})
            if item.get('kind')=='分类':return item['category']
            key=item.get('parent','')
        return ''

    def add_category(self):
        parent=self.selected_category()
        name=simpledialog.askstring('新建分类',f'在 {parent or "我的笔记库"} 下创建子分类：',parent=self.root)
        if name:
            try:self.navigation.create_category(name,parent);self.library_revision+=1;self.render_library()
            except (OSError,ValueError) as error:messagebox.showerror('无法创建分类',str(error))

    def retarget_preferences(self,changes):
        docs=self.preferences.setdefault('documents',{})
        for old,new in changes.items():
            key=encode_path(old)
            if key in docs:docs[encode_path(new)]=docs.pop(key)
        self.persist()

    def move_to_category(self):
        selected=self.library_tree.selection()
        item=self.library_results.get(selected[0],{}) if selected else {}
        if item.get('kind') not in ('笔记','标题'):return
        if getattr(self,'current_document','') and note_key(item['path'])==note_key(self.current_document):
            messagebox.showinfo('先切换笔记','请先保存当前笔记，并在 Xournal++ 中切换到其他笔记，再移动分类。');return
        dialog=tk.Toplevel(self.root);dialog.title('移动到分类');dialog.transient(self.root);dialog.grab_set()
        categories=['未分类']
        if self.navigation.notes_root.exists():
            categories+=sorted(p.relative_to(self.navigation.notes_root).as_posix() for p in self.navigation.notes_root.rglob('*') if p.is_dir() and not p.is_symlink())
        choice=tk.StringVar(value=self.selected_category() or '未分类')
        ttk.Label(dialog,text='选择目标分类（移动笔记文件及相关导航记录）：').pack(padx=12,pady=8)
        ttk.Combobox(dialog,textvariable=choice,values=sorted(set(categories)),state='readonly',width=35).pack(padx=12,pady=5)
        def apply():
            try:
                changes=self.navigation.move_note(item['path'],choice.get());self.retarget_preferences(changes)
                self.library_revision+=1
                self.render_library();self.render_bookmarks();dialog.destroy();self.status.set('笔记与相关书签已移到目标分类。')
            except (OSError,ValueError) as error:messagebox.showerror('移动失败',str(error),parent=dialog)
        ttk.Button(dialog,text='移动',command=apply).pack(pady=10)

    def reindex_notes(self):
        self.sync_library(force=True);self.status.set('正在后台同步 Notes 文件夹…')

    def schedule_library_search(self,*_):
        if self.library_search_timer:self.root.after_cancel(self.library_search_timer)
        self.library_search_timer=self.root.after(150,self.render_library)

    def library_stamp(self):
        notes=tuple((key,note['path'],note['name'],note.get('id'),
                     tuple((h['title'],h['page'],h['level']) for h in note['headings']))
                    for key,note in self.navigation.data['notes'].items())
        marks=tuple(tuple(sorted(mark.items())) for mark in self.navigation.data['bookmarks'])
        folders=None if self.navigation._folder_cache is None else tuple(self.navigation._folder_cache)
        return (self.library_query.get().strip(),self.theme.get(),notes,marks,folders)

    def render_library(self):
        self.library_search_timer=None
        if self.background_sync and not self.library_visible():
            self.library_dirty=True
            return
        self.library_dirty=False
        if self.library_stamp()==self.library_source_signature:return
        query=self.library_query.get().strip()
        items=self.navigation.library_items(query)
        self.library_source_signature=self.library_stamp()
        self.library_results={item['key']:item for item in items}
        signature=(query,self.theme.get(),tuple((item['key'],item['parent'],item['title'],item['page'],item['kind']) for item in items))
        if signature==self.library_render_signature:return
        if not getattr(self,'library_filter',''):
            self.library_open=getattr(self,'library_open',{})
            def capture(parent=''):
                for key in self.library_tree.get_children(parent):
                    self.library_open[key]=bool(self.library_tree.item(key,'open'));capture(key)
            capture()
        selected=self.library_tree.selection()
        previous=getattr(self,'library_row_state',{})
        previous_theme=getattr(self,'library_icon_theme',None)
        query_changed=query!=getattr(self,'library_filter','')
        current={};children={}
        for item in items:
            key=item['key'];parent=item['parent']
            row=(parent,item['title'],item['page'],item['kind']);current[key]=row
            children.setdefault(parent,[]).append(key)
            opened=True if query else getattr(self,'library_open',{}).get(key,True)
            attributes={'text':item['title'],'values':(item['page'],),
                        'image':self.icons.get(item['kind'],self.icons['标题'])}
            if key not in previous:
                self.library_tree.insert(parent,'end',iid=key,open=opened,**attributes)
            else:
                if row!=previous[key] or previous_theme!=self.theme.get():self.library_tree.item(key,**attributes)
                if previous[key][0]!=parent:self.library_tree.move(key,parent,'end')
                if query_changed:self.library_tree.item(key,open=opened)
        # Move surviving notes before deleting their old category, keeping their
        # descendants, selection and expansion state intact.
        for key in previous.keys()-current.keys():
            if self.library_tree.exists(key):self.library_tree.delete(key)
        old_children=getattr(self,'library_children',{})
        for parent,keys in children.items():
            if keys!=old_children.get(parent):self.library_tree.set_children(parent,*keys)
        self.library_row_state=current;self.library_children=children;self.library_icon_theme=self.theme.get()
        if selected and self.library_tree.exists(selected[0]):self.library_tree.selection_set(selected[0])
        self.library_filter=query
        self.library_render_signature=signature

    def expand_library(self,opened):
        def visit(parent=''):
            for key in self.library_tree.get_children(parent):
                self.library_tree.item(key,open=opened);visit(key)
        visit()

    def open_library(self):
        selected=self.library_tree.selection()
        if selected:
            item=self.library_results[selected[0]]
            if item['kind'] in ('库','分类'):
                key=selected[0];self.library_tree.item(key,open=not self.library_tree.item(key,'open'));return
            self.open_note(item['path'],item['page'])

    def remove_note(self):
        selected=self.library_tree.selection()
        if selected:
            item=self.library_results[selected[0]]
            if item['kind'] in ('库','分类'):return
            if messagebox.askyesno('移出笔记库？','只移除索引，不删除笔记文件。',parent=self.root):
                self.navigation.hide_note(item['path']);self.library_revision+=1;self.render_library()

    def open_note(self,path,page,returning=False):
        if not Path(path).is_file():messagebox.showerror('文件不存在','笔记可能被移动或删除，请重新添加。');return
        if self.pending:return
        self.navigation_job={'path':path,'page':page,'returning':returning,'phase':'capture'}
        if not self.request('info',0):self.navigation_job=None

    def go_back(self):
        stack=self.navigation.data.get('back_stack',[])
        if stack:self.open_note(stack[-1]['path'],stack[-1]['page'],returning=True)
        else:self.status.set('暂无可返回的位置。')

    def check_navigation(self):
        if self.navigation_job and self.navigation_job['phase']=='confirm' and not self.pending:self.request('info',0)

    def advance_navigation(self,status):
        job=self.navigation_job
        if not job:return
        if job['phase']=='capture' and status=='当前位置已更新':
            job['origin']=(self.current_document,self.current_page);job['phase']='confirm'
            self.root.after(40,lambda:self.request('open',job['page'],job['path']) if self.navigation_job is job else None)
        elif job['phase']=='confirm':
            if self.current_document and note_key(self.current_document)==note_key(job['path']) and self.current_page==job['page']:
                if job['returning']:
                    stack=self.navigation.data.get('back_stack',[])
                    if stack:stack.pop();self.navigation.save()
                elif job.get('origin')!=(self.current_document,self.current_page):
                    self.navigation.push_back(*job['origin'])
                self.navigation_job=None
                if not self.rows:self.root.after(40,lambda:self.request('refresh',0))
            elif status.startswith('已发送打开请求'):self.root.after(800,self.check_navigation)

    def capture_view(self,force=False):
        if not self.doc_key or (self.search.get().strip() and not force): return
        docs=self.preferences.setdefault('documents',{})
        docs[self.doc_key]={'open':{key:bool(self.tree.item(key,'open')) for key in self.indices if self.tree.exists(key)},
            'selected':list(self.tree.selection()),'scroll':self.tree.yview()[0]}
        while len(docs)>50: del docs[next(iter(docs))]

    def persist(self):
        self.save_timer=None
        self.capture_view()
        if self.root.state()=='normal': self.preferences['geometry']=self.root.geometry()
        self.preferences['topmost']=self.topmost.get()
        self.preferences['theme']=self.theme.get()
        try: save_preferences(self.preferences_path,self.preferences)
        except OSError as error: self.status.set('无法保存窗口设置：'+str(error))

    def schedule_save(self):
        if self.save_timer: self.root.after_cancel(self.save_timer)
        self.save_timer=self.root.after(600,self.persist)

    def close(self):
        self.sync_closed=True
        if self.sync_executor:self.sync_executor.shutdown(wait=False,cancel_futures=True)
        self.persist()
        for timer in self.root.tk.call('after','info'):self.root.after_cancel(timer)
        self.root.destroy()

    def toggle_topmost(self):
        self.root.attributes('-topmost',self.topmost.get());self.schedule_save()

    def schedule_search(self,*_):
        if not getattr(self,'last_query',''):
            self.capture_view(force=True)
        self.last_query=self.search.get().strip()
        if self.search_timer: self.root.after_cancel(self.search_timer)
        self.search_timer=self.root.after(150,self.render)

    def render(self):
        self.search_timer=None
        view=self.preferences.get('documents',{}).get(self.doc_key,{})
        query=self.search.get().strip()
        self.tree.delete(*self.tree.get_children())
        self.indices={}
        for key,parent,index,page,title in filtered_nodes(self.nodes,query):
            self.indices[key]=index
            self.tree.insert(parent,'end',iid=key,text=title,values=(page,),open=True if query else view.get('open',{}).get(key,True))
        for key in view.get('selected',[]):
            if self.tree.exists(key): self.tree.selection_set(key);break
        self.root.update_idletasks()
        if not query: self.tree.yview_moveto(view.get('scroll',0))
        if query and not self.indices: self.status.set('没有匹配的标题。')

    def find_windows(self):
        self.targets=windows()
        self.combo['values']=[title for _,title in self.targets]
        if self.targets: self.combo.current(0)
        else: self.status.set('没有找到 Xournal++，打开笔记后点击“选择窗口”。')
        if len(self.targets)>1:self.combo.pack(fill='x',pady=(6,0))
        else:self.combo.pack_forget()

    def expand(self, opened):
        def visit(parent=''):
            for key in self.tree.get_children(parent):
                self.tree.item(key,open=opened); visit(key)
        visit()
        self.schedule_save()

    def click(self, event):
        element=self.tree.identify_element(event.x,event.y)
        if 'indicator' in element: return
        key=self.tree.identify_row(event.y)
        if key: self.request('jump',int(self.indices[key]))

    def jump_selection(self):
        selected=self.tree.selection()
        if selected: self.request('jump',int(self.indices[selected[0]]))

    def set_level(self,level):
        selected=self.tree.selection()
        if not selected:
            self.status.set('请先选中要修改的标题。');return
        key=selected[0]
        index=int(self.indices[key])
        old=next(row[1] for row in self.rows if int(row[0])==index)
        if old==level:
            self.status.set(f'该标题已经是{level}级。');return
        self.request('level'+str(level),index)

    def level_menu(self,event):
        key=self.tree.identify_row(event.y)
        if key:
            self.tree.selection_set(key)
            self.menu.tk_popup(event.x_root,event.y_root)

    def request(self, action, index, payload=''):
        if self.pending: return False
        selected=self.combo.current()
        if selected < 0 or selected >= len(self.targets):
            messagebox.showinfo('选择窗口','请先选择 Xournal++ 窗口'); return
        if not self.session:
            messagebox.showinfo('尚未连接','先在插件菜单选择“打开/刷新独立大纲窗口”。'); return
        fd,temp=tempfile.mkstemp(dir=ROOT,prefix='PageOutline-command-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
                stream.write(f'{self.session}\t{self.epoch}\t{action}\t{index}\t{quote(payload,safe="")}\n')
            os.replace(temp,COMMAND)
            self.pending=True
            self.level_selection=index if action.startswith('level') else None
            self.sent=time.monotonic()
            self.status.set('等待 Xournal++ 响应…')
            signal_window(self.targets[selected][0])
            return True
        except Exception as error:
            self.pending=False
            Path(temp).unlink(missing_ok=True)
            self.status.set(str(error))

    def poll(self):
        try:
            stat=STATE.stat()
            modified=(stat.st_mtime_ns,stat.st_size)
            if modified==self.state_stat:
                self.finish_poll();return
            session,epoch,status,document,rows,current,count=read_state()
            self.state_stat=modified
            signature=(session,epoch)
            if signature != self.signature:
                doc_key=encode_path(document) if document else '未命名:'+session
                changed=rows!=self.rows or doc_key!=self.doc_key
                if changed:
                    self.capture_view()
                    self.doc_key=doc_key;self.rows=rows;self.nodes=list(stable_nodes(rows))
                    self.render()
                self.session=session; self.epoch=epoch; self.signature=signature
                self.document.set(Path(document).name if document else '当前未命名笔记')
                parent=str(Path(document).parent) if document else '当前文件所在目录'
                self.directory.set(parent if len(parent)<=65 else '…'+parent[-64:])
                self.current_document=document;self.current_page=current
                self.status.set(status if rows or status!='已刷新' else '没有标题。请在笔记中输入 # / ## / ### 加空格的文字标题。')
                if getattr(self,'level_selection',None) and status.startswith('已将'):
                    for key,index in self.indices.items():
                        if int(index)==self.level_selection:
                            self.tree.selection_set(key);self.tree.see(key);break
                self.level_selection=None
                self.pending=False
                self.navigation.record_read(document,current);self.render_recent()
                self.advance_navigation(status)
                if self.bookmark_pending and status=='当前位置已更新':
                    self.root.after(50,self.finish_bookmark)
        except (OSError,ValueError,IndexError):
            pass  # Lua may be replacing the snapshot; retry, retaining the last tree.
        self.finish_poll()

    def finish_poll(self):
        if self.pending and time.monotonic()-self.sent>5:
            self.pending=False
            self.status.set('未收到响应。确认插件已更新并重启；也可在 Xournal++ 按 Ctrl+Alt+Shift+F12。')
        self.root.after(1000 if self.root.state()=='iconic' else 250,self.poll)


def main():
    import sys
    if '--self-test' in sys.argv:
        expected=[('1','',1,'章'),('2','1',2,'节'),('3','2',2,'项'),('4','',4,'末章')]
        assert list(hierarchy([('1',1,1,'章'),('2',2,2,'节'),('3',3,2,'项'),('4',1,4,'末章')]))==expected
        with tempfile.TemporaryDirectory(prefix='pageoutline-selftest-') as temp:
            global STATE
            previous_state=STATE
            STATE=Path(temp)/'state.tsv'
            STATE.write_text('PageOutlineTree-v1\ttest\t1\n已刷新\ttest.xopp\n1\t1\t章\n2\t2\t节\n3\t2\t项\n1\t4\t末章\n',encoding='utf-8')
            prefs=Path(temp)/'prefs.json'
            save_preferences(prefs,{'geometry':'440x640+80+90','topmost':False,'documents':{}})
            root=tk.Tk();root.withdraw()
            app=OutlineApp(root,prefs,Path(temp),background_sync=False);root.update()
            assert len(app.modules.tabs())==2 and len(app.tabs.tabs())==2 and len(app.library_tabs.tabs())==2
            assert app.library_tree['columns']==('page',)
            assert ttk.Style(root).lookup('Treeview','fieldbackground')=='#242931'
            app.modules.select(1);root.update();assert app.library_visible()
            app.modules.select(0);app.tabs.select(1);root.update();assert str(app.level_buttons[0]['state'])=='disabled'
            app.tabs.select(0);root.update();assert str(app.level_buttons[0]['state'])=='normal'
            app.theme.set('浅色');app.change_theme();assert ttk.Style(root).lookup('Treeview','fieldbackground')=='#ffffff'
            app.theme.set('深色');app.change_theme()
            key=app.nodes[0][0]
            app.tree.item(key,open=False)
            app.topmost.set(True);app.toggle_topmost();app.persist()
            app.search.set('项')
            if app.search_timer:root.after_cancel(app.search_timer)
            app.render();assert len(app.tree.get_children(''))==1
            child=app.nodes[2][0];app.tree.selection_set(child)
            commands=[];app.request=lambda action,index:commands.append((action,index))
            app.jump_selection();assert commands==[('jump',3)]
            app.set_level(1);assert commands[-1]==('level1',3)
            before=len(commands);app.set_level(3);assert len(commands)==before
            app.search.set('无匹配')
            if app.search_timer:root.after_cancel(app.search_timer)
            app.render();assert not app.tree.get_children('')
            import gzip
            fixture=Path(temp)/'中文笔记.xopp'
            fixture.write_bytes(gzip.compress('<xournal><page><layer><text x="1" y="1"># Library title</text></layer></page><page><layer/></page></xournal>'.encode('utf-8')))
            original=fixture.read_bytes()
            app.navigation.add_notes([fixture]);app.render_library()
            roots=app.library_tree.get_children();assert len(roots)==1
            assert len(app.library_tree.get_children(roots[0]))==1
            app.library_query.set('Library title');app.render_library();assert len(app.library_tree.get_children())==1
            child=next(key for key,item in app.library_results.items() if item['kind']=='标题')
            assert app.library_results[child]['title']=='Library title'
            app.expand_library(False);assert not app.library_tree.item(roots[0],'open')
            app.expand_library(True);assert app.library_tree.item(roots[0],'open')
            app.current_document=str(fixture);app.current_page=2
            ask=simpledialog.askstring
            simpledialog.askstring=lambda *a,**kw:'复习书签'
            try:app.finish_bookmark()
            finally:simpledialog.askstring=ask
            bookmark=app.navigation.data['bookmarks'][0]
            app.bookmark_tree.selection_set(bookmark['id'])
            app.request=lambda action,index,payload='':commands.append((action,index,payload)) or True
            app.open_bookmark();assert commands[-1]==('info',0,'')
            app.advance_navigation('当前位置已更新');time.sleep(.06);root.update()
            assert commands[-1]==('open',2,str(fixture.resolve()))
            app.advance_navigation('已跳转到第2页')
            app.current_page=1;app.open_note(str(fixture),2)
            app.advance_navigation('当前位置已更新');time.sleep(.06);root.update()
            assert not app.navigation.data.get('back_stack')
            app.advance_navigation('当前位置已更新')
            assert not app.navigation.data.get('back_stack')
            app.current_page=2;app.advance_navigation('已跳转到第2页')
            assert app.navigation.data['back_stack'][-1]['page']==1
            app.navigation.record_read(str(fixture),2);app.render_recent()
            assert app.recent_tree.item('0','values')==('2',)
            app.go_back();app.advance_navigation('当前位置已更新');time.sleep(.06);root.update()
            assert app.navigation.data['back_stack'][-1]['page']==1
            app.current_page=1;app.advance_navigation('已跳转到第1页')
            assert not app.navigation.data['back_stack']
            assert fixture.read_bytes()==original
            app.close()
            root=tk.Tk();root.withdraw();restored=OutlineApp(root,prefs,Path(temp),background_sync=False);root.update()
            assert not restored.tree.item(key,'open')
            assert restored.topmost.get() and root.attributes('-topmost')
            assert restored.theme.get()=='深色'
            assert restored.preferences['geometry']=='440x640+80+90'
            assert restored.navigation.data['bookmarks'][0]['title']=='复习书签'
            assert len(restored.bookmark_tree.get_children())==1
            restored.close()
            managed=Path(temp)/'Notes'/'新增分类'/'子分类'/'磁盘笔记.xopp';managed.parent.mkdir(parents=True)
            managed.write_bytes(gzip.compress('<xournal><page><layer><text># Filesystem title</text></layer></page></xournal>'.encode()))
            root=tk.Tk();root.withdraw();live=OutlineApp(root,prefs,Path(temp));live.modules.select(1)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and not any(item['title']=='Filesystem title' for item in live.library_results.values()):
                root.update();time.sleep(.01)
            assert any(item['title']=='Filesystem title' for item in live.library_results.values())
            assert any(item['title']=='子分类' for item in live.library_results.values())
            category=next(key for key,item in live.library_results.items() if item['title']=='新增分类')
            live.library_tree.selection_set(category)
            ask=simpledialog.askstring;simpledialog.askstring=lambda *args,**kwargs:'改名分类'
            try:live.rename_library_item()
            finally:simpledialog.askstring=ask
            assert (Path(temp)/'Notes'/'改名分类'/'子分类'/'磁盘笔记.xopp').exists()
            live.navigation.create_category('拖动目标');live.render_library()
            target=next(key for key,item in live.library_results.items() if item['title']=='拖动目标')
            live.drag_item=next(item for item in live.library_results.values() if item['kind']=='笔记' and item['title']=='磁盘笔记')
            identify=live.library_tree.identify_row;live.library_tree.identify_row=lambda y:target
            from types import SimpleNamespace
            live.dragging=True
            try:live.drag_release(SimpleNamespace(y=0))
            finally:live.library_tree.identify_row=identify
            assert (Path(temp)/'Notes'/'拖动目标'/'磁盘笔记.xopp').exists()
            live.close();STATE=previous_state
        if '--test-result' in sys.argv:
            Path(sys.argv[sys.argv.index('--test-result')+1]).write_text('PASS: dark/light themes and persistence, two modules/subtabs, library icons and name/page columns, outline/bookmark regression, background sync, category drag/rename, reading history and confirmed return',encoding='utf-8')
        return
    ROOT.mkdir(parents=True,exist_ok=True)
    instance=claim_instance()
    if instance is None:return
    try:
        root=tk.Tk(); OutlineApp(root); root.mainloop()
    finally:
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(instance)


if __name__=='__main__':
    try: main()
    except Exception as error:
        import sys
        if '--self-test' in sys.argv and '--test-result' in sys.argv:
            import traceback
            Path(sys.argv[sys.argv.index('--test-result')+1]).write_text(traceback.format_exc(),encoding='utf-8')
            raise SystemExit(1)
        raise
