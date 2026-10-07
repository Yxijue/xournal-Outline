"""Nine-slice rounded borders for native ttk widgets; behavior stays native."""
import itertools
import tkinter as tk

_generation=itertools.count()

def install_round_borders(root,style,colors):
    palette=tuple(sorted(colors.items()))
    themes=getattr(root,'_rounded_theme_cache',{})
    if palette in themes:
        images,layouts=themes[palette]
        for name,layout in layouts.items():style.layout(name,layout)
        return images
    prefix=f'Round{next(_generation)}';images=[];cache={}
    def rgb(value):return tuple(int(value[i:i+2],16) for i in (1,3,5))
    def picture(fill,outline,size=256):
        key=(fill,outline,size)
        if key in cache:return cache[key]
        # Tk tiles the centre/edges of a nine-slice image instead of stretching
        # them. A tiny centre generates thousands of GDI draws on large panes.
        radius=7;outer=rgb(colors['bg']);face=rgb(fill);edge=rgb(outline)
        image=tk.PhotoImage(master=root,width=size,height=size)
        image.put(outline,to=(0,0,size,size))
        image.put(fill,to=(1,1,size-1,size-1))
        def inside(x,y,inset):
            low=inset;high=size-inset;r=radius-inset
            if not low<=x<high or not low<=y<high:return False
            cx=min(max(x,low+r),high-r);cy=min(max(y,low+r),high-r)
            return (x-cx)**2+(y-cy)**2<=r*r
        # Only the four corners require antialiasing; fill the flat areas once.
        for origin_x,origin_y in ((0,0),(size-8,0),(0,size-8),(size-8,size-8)):
          rows=[]
          for y in range(origin_y,origin_y+8):
            row=[]
            for x in range(origin_x,origin_x+8):
                total=[0,0,0]
                for dx,dy in ((.25,.25),(.75,.25),(.25,.75),(.75,.75)):
                    color=face if inside(x+dx,y+dy,1) else edge if inside(x+dx,y+dy,0) else outer
                    total=[a+b for a,b in zip(total,color)]
                row.append('#'+''.join(f'{round(v/4):02x}' for v in total))
            rows.append('{'+ ' '.join(row)+'}')
          image.put(' '.join(rows),to=(origin_x,origin_y))
        images.append(image);cache[key]=image;return image
    def element(name,fill,selected=False,padding=(4,3,4,3)):
        size=256 if name in ('Treeview.field','Notebook.client') else 64
        normal=picture(fill,colors['border'],size);hover=picture(colors['hover'],colors['border'],size)
        focus=picture(fill,colors['accent'],size);pressed=picture(colors['selected'],colors['border'],size)
        states=[('pressed',pressed)]
        if selected:states.append(('selected',pressed))
        states.extend([('focus',focus),('active',hover)])
        full=prefix+'.'+name
        style.element_create(full,'image',normal,*states,border=(8,8,8,8),
                             width=24,height=24,padding=padding,sticky='nsew')
        return full
    button=element('Button.border',colors['panel'],padding=(2,2,2,2))
    style.layout('TButton',[(button,{'sticky':'nsew','children':[
        ('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
    entry=element('Entry.field',colors['panel'],padding=(3,2,3,2))
    style.layout('TEntry',[(entry,{'sticky':'nsew','children':[
        ('Entry.padding',{'sticky':'nsew','children':[('Entry.textarea',{'sticky':'nsew'})]})]})])
    combo=element('Combobox.field',colors['panel'],padding=(5,2,6,2))
    style.layout('TCombobox',[(combo,{'sticky':'nsew','children':[
        ('Combobox.downarrow',{'side':'right','sticky':''}),
        ('Combobox.padding',{'sticky':'nsew','children':[('Combobox.textarea',{'sticky':'nsew'})]})]})])
    client=element('Notebook.client',colors['bg'],padding=(5,5,5,5))
    style.layout('TNotebook',[(client,{'sticky':'nsew'})])
    tab=element('Notebook.tab',colors['bg'],selected=True,padding=(1,1,1,1))
    style.layout('TNotebook.Tab',[(tab,{'sticky':'nsew','children':[
        ('Notebook.padding',{'sticky':'nsew','children':[('Notebook.label',{'sticky':''})]})]})])
    # Explicit layouts prevent derived module styles from retaining the theme's native tab border.
    style.layout('Module.TNotebook',style.layout('TNotebook'))
    style.layout('Module.TNotebook.Tab',style.layout('TNotebook.Tab'))
    field=element('Treeview.field',colors['panel'],padding=(5,5,5,5))
    style.layout('Treeview',[(field,{'sticky':'nsew','children':[
        ('Treeview.padding',{'sticky':'nsew','children':[('Treeview.treearea',{'sticky':'nsew'})]})]})])
    # Indicators are small fixed-size images, independently of border tiles.
    unchecked=picture(colors['panel'],colors['border'],size=24).subsample(2)
    checked=picture(colors['selected'],colors['accent'],size=24).subsample(2)
    for x,y in ((3,6),(4,7),(5,8),(6,7),(7,6),(8,5),(9,4)):
        checked.put(colors['text'],to=(x,y,x+1,y+1))
    images.extend((unchecked,checked))
    indicator=prefix+'.Checkbutton.indicator'
    style.element_create(indicator,'image',unchecked,('selected',checked),padding=(0,0,4,0),sticky='')
    style.layout('TCheckbutton',[('Checkbutton.padding',{'sticky':'nsew','children':[
        (indicator,{'side':'left','sticky':''}),('Checkbutton.label',{'side':'left','sticky':'nsew'})]})])
    names=('TButton','TEntry','TCombobox','TNotebook','TNotebook.Tab',
           'Module.TNotebook','Module.TNotebook.Tab','Treeview','TCheckbutton')
    themes[palette]=(images,{name:style.layout(name) for name in names})
    root._rounded_theme_cache=themes
    return images
