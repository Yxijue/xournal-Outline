"""Local page bookmarks and saved .xopp heading index; source notes are read only."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import shutil
import time
from portable_paths import INSTALL_ROOT, encode_path, decode_path
import xml.etree.ElementTree as ET


def note_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def index_note(path):
    path=Path(path).resolve()
    if path.suffix.lower()!='.xopp': raise ValueError('请选择 .xopp 笔记')
    # Streaming XML: image payloads are discarded after each page.
    headings=[];pages=0
    with gzip.open(path,'rb') as stream:
        for _,element in ET.iterparse(stream,events=('end',)):
            if element.tag=='page':
                pages+=1
                for layer in element.findall('layer'):
                    for text in layer.findall('text'):
                        content=''.join(text.itertext())
                        match=re.match(r'^\s*(#{1,3})\s+([^\r\n]+)',content)
                        if match:
                            headings.append({'level':len(match[1]),'title':match[2].strip(),
                                'page':pages,'x':float(text.get('x','0')),'y':float(text.get('y','0'))})
                element.clear()
    headings.sort(key=lambda h:(h['page'],h['y'],h['x'],h['title']))
    stat=path.stat()
    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    return {'path':str(path),'name':path.stem,'pages':pages,'headings':headings,
        'mtime':stat.st_mtime_ns,'size':stat.st_size,'file_id':f'{stat.st_dev}:{stat.st_ino}',
        'digest':digest,'id':uuid.uuid4().hex}


class NavigationStore:
    def __init__(self,path,install_root=INSTALL_ROOT):
        self.path=Path(path)
        self.install_root=Path(install_root)
        self.notes_root=self.install_root/'Notes'
        self._folder_cache=None
        self.data={'version':1,'notes':{},'bookmarks':[]}
        if self.path.exists():
            data=json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') not in (1,2) or not isinstance(data.get('notes'),dict) or not isinstance(data.get('bookmarks'),list):
                raise ValueError('导航库数据格式不正确，请备份后检查数据文件')
            self.data=data
            if data['version']==2:
                for record in [*data['notes'].values(),*data['bookmarks']]:
                    record['path']=decode_path(record['path'],self.install_root)
                data['notes']={note_key(note['path']):note for note in data['notes'].values()}
                data['hidden_notes']=[decode_path(value,self.install_root) for value in data.get('hidden_notes',[])]
                for field in ('recent','back_stack','missing_notes'):
                    for record in data.get(field,[]):record['path']=decode_path(record['path'],self.install_root)
        self.last_retargets={}

    def retarget_records(self,changes):
        normalized={note_key(old):new for old,new in changes.items()}
        for field in ('bookmarks','recent','back_stack','missing_notes'):
            for record in self.data.get(field,[]):
                if note_key(record['path']) in normalized:record['path']=normalized[note_key(record['path'])]
        self.data['hidden_notes']=[normalized.get(note_key(path),path) for path in self.data.get('hidden_notes',[])]

    def record_read(self,path,page):
        if not path.lower().endswith('.xopp') or not Path(path).is_file():return
        recent=self.data.setdefault('recent',[]);key=note_key(path)
        if recent and note_key(recent[0]['path'])==key and recent[0]['page']==page:return
        self.data['recent']=[{'path':str(Path(path).resolve()),'page':page,'time':time.time()}]+[r for r in recent if note_key(r['path'])!=key][:29]
        self.save()

    def push_back(self,path,page):
        if not path.lower().endswith('.xopp'):return
        stack=self.data.setdefault('back_stack',[])
        entry={'path':str(Path(path).resolve()),'page':page}
        if not stack or stack[-1]!=entry:stack.append(entry)
        self.data['back_stack']=stack[-50:];self.save()

    def scan_disk(self,force=False):
        """Worker-only read phase; never mutate state or write notebook files."""
        folders=[];found=set();updates={};errors=[];complete=True
        hidden={note_key(value) for value in self.data.get('hidden_notes',[])}
        root=self.notes_root.resolve()
        if not root.exists():return {'folders':[],'found':set(),'updates':{},'errors':[],'complete':False}
        def failed(error):
            nonlocal complete
            complete=False;errors.append(str(error))
        for current,dirs,files in os.walk(root,onerror=failed,followlinks=False):
            current=Path(current)
            dirs[:]=sorted(name for name in dirs if not (current/name).is_symlink() and (current/name).resolve().is_relative_to(root))
            if current!=root:folders.append(current.relative_to(root).as_posix())
            for name in sorted(files):
                path=current/name
                if path.suffix.lower()!='.xopp' or path.is_symlink():continue
                key=note_key(path);found.add(key)
                if key in hidden:continue
                try:
                    stat=path.stat();old=self.data['notes'].get(key)
                    if not force and old and old.get('digest') and old.get('mtime')==stat.st_mtime_ns and old.get('size')==stat.st_size:continue
                    note=index_note(path);after=path.stat()
                    if (stat.st_mtime_ns,stat.st_size)!=(after.st_mtime_ns,after.st_size):continue
                    updates[key]=note
                except (OSError,ValueError,EOFError,ET.ParseError) as error:errors.append(f'{name}: {error}')
        return {'folders':folders,'found':found,'updates':updates,'errors':errors,'complete':complete}

    def apply_disk_scan(self,result):
        changed=self._folder_cache!=result['folders'];self._folder_cache=result['folders']
        previous=json.loads(json.dumps(self.data))
        self.last_retargets={}
        missing=[n for key,n in self.data['notes'].items() if key not in result['found'] and Path(n['path']).resolve().is_relative_to(self.notes_root.resolve())]
        candidates=missing+self.data.get('missing_notes',[])
        initial_keys=set(self.data['notes'])
        used=set()
        for key,note in result['updates'].items():
            old=self.data['notes'].get(key)
            if old:note={**note,'id':old.get('id',note['id'])}
            elif result['complete']:
                matches=[n for n in missing if note_key(n['path']) not in used and n.get('file_id')==note.get('file_id')]
                if not matches:
                    matches=[n for n in candidates if note_key(n['path']) not in used and n.get('digest')==note.get('digest')]
                    # Identical copies cannot reliably identify a moved original.
                    if sum(n.get('digest')==note.get('digest') for k,n in result['updates'].items() if k not in initial_keys)!=1:matches=[]
                unique={note_key(n['path']):n for n in matches}
                if len(unique)==1:
                    old=next(iter(unique.values()));used.add(note_key(old['path']))
                    note={**note,'id':old.get('id',note['id'])};self.last_retargets[old['path']]=note['path']
            if self.data['notes'].get(key)!=note:self.data['notes'][key]=note;changed=True
        if result['complete']:
            for key,note in list(self.data['notes'].items()):
                if Path(note['path']).resolve().is_relative_to(self.notes_root.resolve()) and key not in result['found']:
                    del self.data['notes'][key];changed=True
            self.data['missing_notes']=[n for n in candidates if note_key(n['path']) not in used][-500:]
            self.retarget_records(self.last_retargets)
        if previous!=self.data:
            try:self.save()
            except Exception:self.data=previous;raise
        return changed

    def hide_note(self,path):
        self.data.setdefault('hidden_notes',[]).append(str(Path(path).resolve()))
        self.data['notes'].pop(note_key(path),None);self.save()

    def category_directory(self,category=''):
        target=(self.notes_root/category).resolve()
        if not target.is_relative_to(self.notes_root.resolve()):raise ValueError('分类必须位于 Notes 文件夹内')
        return target

    def create_category(self,name,parent=''):
        name=name.strip()
        if not name or name in ('.','..') or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith(('.', ' ')):
            raise ValueError('请输入有效的文件夹名称，不包含路径分隔符')
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
            raise ValueError('此名称为 Windows 保留名称')
        target=self.category_directory(str(Path(parent)/name))
        target.mkdir(parents=True,exist_ok=True)
        self._folder_cache=None
        return target.relative_to(self.notes_root.resolve()).as_posix()

    def import_notes(self,paths,category='未分类'):
        folder=self.category_directory(category);folder.mkdir(parents=True,exist_ok=True)
        changes=[];created=[]
        try:
            for value in paths:
                source=Path(value).resolve();index_note(source)
                if source.parent==folder:
                    target=source
                else:
                    target=folder/source.name;number=2
                    while target.exists():
                        target=folder/f'{source.stem} ({number}){source.suffix}';number+=1
                    with source.open('rb') as incoming,target.open('xb') as outgoing:
                        created.append(target);shutil.copyfileobj(incoming,outgoing)
                    shutil.copystat(source,target)
                changes.append((source,target,index_note(target)))
            previous=json.loads(json.dumps(self.data))
            for source,target,note in changes:
                self.data['hidden_notes']=[value for value in self.data.get('hidden_notes',[]) if note_key(value)!=note_key(target)]
                old=self.data['notes'].pop(note_key(source),None)
                if old:note['id']=old.get('id',note['id'])
                self.data['notes'][note_key(target)]=note
            self.retarget_records({str(source):str(target) for source,target,_ in changes})
            try:self.save()
            except Exception:self.data=previous;raise
            self._folder_cache=None
            return {str(source):str(target) for source,target,_ in changes}
        except Exception:
            for path in created:path.unlink(missing_ok=True)
            raise

    def move_note(self,path,category):
        source=Path(path).resolve()
        if not source.is_relative_to(self.notes_root.resolve()):raise ValueError('请先导入副本，再移动分类')
        if source.parent==self.category_directory(category):return {}
        changes=self.import_notes([source],category)
        # Keep the source if deletion fails; navigation already safely targets copy.
        source.unlink()
        return changes

    def rename_category(self,category,name):
        source=self.category_directory(category)
        if source==self.notes_root.resolve():raise ValueError('不能改名 Notes 根目录')
        # Validate using the same rules as new categories without creating it.
        if not name.strip() or name in ('.','..') or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith(('.', ' ')):
            raise ValueError('分类名称无效')
        if name.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:raise ValueError('分类名称为保留名称')
        return self.relocate_category(source,source.with_name(name.strip()))

    def archive_category(self,category):
        source=self.category_directory(category)
        unclassified=self.category_directory('未分类')
        if source==self.notes_root.resolve() or source==unclassified or unclassified.is_relative_to(source):
            raise ValueError('不能移除根目录或未分类目录')
        if not any(source.iterdir()):source.rmdir();self._folder_cache=None;return {}
        unclassified.mkdir(exist_ok=True)
        target=unclassified/source.name;number=2
        while target.exists():target=unclassified/f'{source.name} ({number})';number+=1
        return self.relocate_category(source,target)

    def relocate_category(self,source,target):
        root=self.notes_root.resolve();source=Path(source).resolve();target=Path(target).resolve()
        if not source.is_relative_to(root) or not target.is_relative_to(root) or target.is_relative_to(source):raise ValueError('分类路径无效')
        if target.exists():raise ValueError('目标分类已存在，未覆盖')
        previous=json.loads(json.dumps(self.data))
        paths={str(p.resolve()) for p in source.rglob('*.xopp')}
        for field in ('notes','bookmarks','recent','back_stack','missing_notes'):
            records=self.data.get(field,{})
            for record in records.values() if isinstance(records,dict) else records:
                if Path(record['path']).resolve().is_relative_to(source):paths.add(record['path'])
        paths.update(p for p in self.data.get('hidden_notes',[]) if Path(p).resolve().is_relative_to(source))
        changes={path:str(target/Path(path).resolve().relative_to(source)) for path in paths}
        source.rename(target)
        try:
            self.retarget_records(changes)
            for note in self.data['notes'].values():note['path']=changes.get(note['path'],note['path'])
            self.data['notes']={note_key(n['path']):n for n in self.data['notes'].values()}
            self.save();self._folder_cache=None
        except Exception:
            self.data=previous;target.rename(source);raise
        return changes

    def rename_note(self,path,name):
        source=Path(path).resolve()
        if not source.is_relative_to(self.notes_root.resolve()):raise ValueError('只能重命名 Notes 内的笔记')
        if not name.strip() or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or name.endswith(('.', ' ')):raise ValueError('笔记名称无效')
        target=source.with_name(name if name.lower().endswith('.xopp') else name+'.xopp')
        if target.exists():raise ValueError('同名笔记已存在，未覆盖')
        previous=json.loads(json.dumps(self.data));source.rename(target)
        changes={str(source):str(target)}
        try:
            note=self.data['notes'].pop(note_key(source),index_note(target));note.update(path=str(target),name=target.stem)
            self.data['notes'][note_key(target)]=note;self.retarget_records(changes);self.save()
        except Exception:self.data=previous;target.rename(source);raise
        return changes

    def library_items(self,query=''):
        root={'key':'library-root','parent':'','kind':'库','title':'我的笔记库','note':'','page':'','category':''}
        result=[root];folders={}
        def category_item(category):
            if not category:return 'library-root'
            if category in folders:return folders[category]
            parent=Path(category).parent.as_posix();parent='' if parent=='.' else parent
            parent_key=category_item(parent)
            key='category-'+hashlib.sha256(category.encode('utf-8')).hexdigest()
            folders[category]=key
            result.append({'key':key,'parent':parent_key,'kind':'分类','title':Path(category).name,'note':'','page':'','category':category})
            return key
        if self._folder_cache is None:
            self._folder_cache=[]
            if self.notes_root.exists():
                self._folder_cache=[directory.relative_to(self.notes_root).as_posix() for directory in sorted(self.notes_root.rglob('*'))
                    if directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(self.notes_root.resolve())]
        for category in self._folder_cache:category_item(category)
        rows=self.tree_items()
        if query:
            known={item['key'] for item in rows}
            rows.extend(item for item in self.tree_items(query) if item['key'] not in known)
        for item in rows:
            if not item['parent']:
                path=Path(item['path']).resolve()
                category=path.parent.relative_to(self.notes_root.resolve()).as_posix() if path.is_relative_to(self.notes_root.resolve()) else '外部笔记'
                category='' if category=='.' else category
                item['parent']=category_item(category)
                item['category']=category
        result.extend(rows)
        if not query:return result
        query=query.strip().casefold();by_key={item['key']:item for item in result};included=set()
        for item in result:
            if query in item['title'].casefold() or item['kind']=='书签':
                included.add(item['key'])
                if item['kind'] in ('分类','笔记'):
                    descendants={item['key']}
                    for child in result:
                        if child['parent'] in descendants:descendants.add(child['key'])
                    included.update(descendants)
        for key in list(included):
            while key:included.add(key);key=by_key[key]['parent']
        return [item for item in result if item['key'] in included]

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,temp=tempfile.mkstemp(prefix='.navigation-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stored={'version':2,'notes':{},'bookmarks':[], 'hidden_notes':[encode_path(value,self.install_root) for value in self.data.get('hidden_notes',[])]}
                for note in self.data['notes'].values():
                    path=encode_path(note['path'],self.install_root)
                    stored['notes'][os.path.normcase(path)]={**note,'path':path}
                stored['bookmarks']=[{**mark,'path':encode_path(mark['path'],self.install_root)} for mark in self.data['bookmarks']]
                for field in ('recent','back_stack','missing_notes'):
                    stored[field]=[{**record,'path':encode_path(record['path'],self.install_root)} for record in self.data.get(field,[])]
                json.dump(stored,stream,ensure_ascii=False,indent=2)
            os.replace(temp,self.path)
        finally:Path(temp).unlink(missing_ok=True)

    def add_notes(self,paths):
        try:results=[index_note(path) for path in paths]
        except ET.ParseError as error:raise ValueError('笔记 XML 数据损坏：'+str(error)) from error
        for note in results:self.data['notes'][note_key(note['path'])]=note
        self.save()

    def bookmark(self,path,page,title,tag='普通'):
        if Path(path).suffix.lower()!='.xopp' or not Path(path).is_file():
            raise ValueError('请先保存当前笔记为 .xopp 文件')
        if page<1 or not title.strip():raise ValueError('书签标题或页码无效')
        result={'id':uuid.uuid4().hex,'path':str(Path(path).resolve()),'page':page,'title':title.strip(),'tag':tag}
        self.data['bookmarks'].append(result);self.save();return result

    def search(self,query=''):
        query=query.strip().casefold();result=[]
        for note in self.data['notes'].values():
            if not query or query in note['name'].casefold():
                result.append({'kind':'笔记','path':note['path'],'page':1,'title':note['name'],'note':note['name']})
            for heading in note['headings']:
                if not query or query in heading['title'].casefold():
                    result.append({'kind':'标题','path':note['path'],'page':heading['page'],'title':heading['title'],'note':note['name']})
        for bookmark in self.data['bookmarks']:
            if query and query in (bookmark['title']+' '+bookmark['tag']).casefold():
                result.append({**bookmark,'kind':'书签','note':Path(bookmark['path']).stem})
        return result

    def tree_items(self,query=''):
        query=query.strip().casefold()
        result=[]
        notes=dict(self.data['notes'])
        marks=[m for m in self.data['bookmarks'] if query and query in (m['title']+' '+m['tag']).casefold()]
        for mark in marks:
            notes.setdefault(note_key(mark['path']),{'path':mark['path'],'name':Path(mark['path']).stem,'headings':[]})
        for note in notes.values():
            root='note-'+hashlib.sha256(note_key(note['path']).encode('utf-8')).hexdigest()
            items=[{'key':root,'parent':'','kind':'笔记','path':note['path'],'page':1,'title':note['name'],'note':note['name']}]
            parents={};counts={}
            for heading in note['headings']:
                level=heading['level']
                parent=next((parents[k] for k in range(level-1,0,-1) if k in parents),root)
                token=(parent,level,heading['title'])
                counts[token]=counts.get(token,0)+1
                key='heading-'+hashlib.sha256(json.dumps([*token,counts[token]],ensure_ascii=False).encode('utf-8')).hexdigest()
                items.append({**heading,'key':key,'parent':parent,'kind':'标题','path':note['path'],'note':note['name']})
                parents={k:v for k,v in parents.items() if k<level};parents[level]=key
            for mark in marks:
                if note_key(mark['path'])==note_key(note['path']):
                    items.append({**mark,'key':'bookmark-'+mark['id'],'parent':root,'kind':'书签','note':note['name']})
            if not query or query in note['name'].casefold():
                result.extend(items);continue
            by_key={item['key']:item for item in items};include=set()
            for item in items:
                if query in item['title'].casefold() or item['kind']=='书签':
                    key=item['key']
                    while key:include.add(key);key=by_key[key]['parent']
            result.extend(item for item in items if item['key'] in include)
        return result
