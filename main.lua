-- Headings are ordinary document text: # title, ## title, ### title.
-- No external dependencies, sidecar files, or stored element pointers.

local function notify(message)
    if type(app.openDialog) == 'function' then
        app.openDialog(message, {'确定'}, '')
    elseif type(app.msgbox) == 'function' then
        app.msgbox(message, {'确定'})
    else
        print(message)
    end
end

local function guarded(fn)
    local ok, err = pcall(fn)
    if not ok then notify('大纲插件：' .. tostring(err)) end
end

local function requireApi(names)
    for _, name in ipairs(names) do
        assert(type(app[name]) == 'function', '当前版本缺少 app.' .. name .. '，请更新 Xournal++。')
    end
end

local function readAllTexts()
    local ok, texts = pcall(app.getTexts, 'all')
    if ok then return texts end
    -- Only fall back for an unsupported scope, not unrelated API failures.
    if not tostring(texts):find('Unknown argument', 1, true) then error(texts) end
    requireApi({'setCurrentPage', 'setCurrentLayer'})
    local doc = app.getDocumentStructure()
    local originalPage = doc.currentPage
    local result = {}
    local visited = {}
    local function restore()
        for _, page in ipairs(visited) do
            app.setCurrentPage(page)
            app.setCurrentLayer(doc.pages[page].currentLayer, false)
        end
        app.setCurrentPage(originalPage)
    end
    local success, failure = pcall(function()
        for page, info in ipairs(doc.pages) do
            visited[#visited+1] = page
            app.setCurrentPage(page)
            for layer=1,#info.layers do
                app.setCurrentLayer(layer, false)
                for _, box in ipairs(app.getTexts('layer')) do
                    -- Copy values, avoiding document element references.
                    result[#result+1] = {text=box.text, x=box.x, y=box.y, page=page,layer=layer,
                        font=box.font,color=box.color,wrap=box.wrap,ref=box.ref}
                end
            end
        end
    end)
    local restored, restoreError = pcall(restore)
    if not restored then error('恢复页面/图层失败：' .. tostring(restoreError)) end
    if not success then error(failure) end
    return result
end

local function scan()
    requireApi({'getTexts', 'getDocumentStructure'})
    local result = {}
    for _, box in ipairs(readAllTexts()) do
        local marks, title = (box.text or ''):match('^%s*(#+)%s+([^\r\n]+)')
        if marks and #marks <= 3 and box.page then
            title = title:gsub('%s+$', '')
            if title ~= '' then
                result[#result + 1] = {level=#marks, title=title, page=box.page,
                    x=box.x or 0, y=box.y or 0,layer=box.layer, text=box.text,
                    font=box.font,color=box.color,wrap=box.wrap,ref=box.ref}
            end
        end
    end
    table.sort(result, function(a, b)
        if a.page ~= b.page then return a.page < b.page end
        if a.y ~= b.y then return a.y < b.y end
        if a.x ~= b.x then return a.x < b.x end
        return a.title < b.title
    end)
    return result
end

function outlineAdd(level)
    guarded(function()
        requireApi({'addTexts', 'getDocumentStructure', 'getTexts'})
        level = tonumber(level) or 1
        local doc = app.getDocumentStructure()
        local page = assert(doc.pages[doc.currentPage], '没有当前页面。')
        local y = 36
        for _, e in ipairs(scan()) do
            if e.page == doc.currentPage then y=math.max(y,e.y+36) end
        end
        assert(page.pageWidth > 80 and y+32 < page.pageHeight, '页面顶部没有足够空间，请用文字工具在空白位置手动添加标题。')
        app.addTexts({texts={{text=string.rep('#',level)..' 请在这里填写标题',
            x=36,y=y,font={name='Sans',size=20-level*2},color=0x245A81,
            wrap=page.pageWidth-72}},allowUndoRedoAction='grouped'})
        notify('已插入标题模板。请用文字工具点击模板并修改标题，然后保存文档。\n模板位置可能与原有笔记重叠，可用选择工具移动。')
    end)
end

function outlineInsertToc()
    guarded(function()
        requireApi({'addTexts'})
        local list=scan()
        if #list == 0 then notify('没有标题可生成目录。'); return end
        local doc=app.getDocumentStructure()
        local page=assert(doc.pages[doc.currentPage], '没有当前页面。')
        local lines={'目录'}
        local longest=0
        for _, e in ipairs(list) do
            local line=string.rep('    ',e.level-1)..e.title..' …… '..e.page
            lines[#lines+1]=line
            longest=math.max(longest, utf8.len(line) or #line)
        end
        assert(page.pageWidth>80 and #lines*19+72 < page.pageHeight and longest*12 < page.pageWidth-72,
            '目录超过一页可用空间，请缩短标题或使用大纲窗口浏览。')
        app.addTexts({texts={{text=table.concat(lines,'\n'),x=36,y=36,
            font={name='Sans',size=12},color=0x245A81}},allowUndoRedoAction='grouped'})
        notify('已插入目录文字。目录是静态快照；标题或页码变化后需删除旧目录并重新生成。\n建议在空白页操作，可用 Ctrl+Z 撤销。')
    end)
end

function outlineExportPdf()
    guarded(function()
        requireApi({'export'})
        if #scan() == 0 then notify('没有标题，请先添加 # / ## / ### 标题。'); return end
        if type(app.fileDialogSave) == 'function' then
            app.fileDialogSave('outlineExportChosen', '笔记.pdf')
        elseif type(app.saveAs) == 'function' then
            outlineExportChosen(app.saveAs('笔记.pdf'))
        else error('当前版本没有保存文件对话框接口。') end
    end)
end

local pluginSource = debug.getinfo(1, 'S').source
local pluginDirectory = pluginSource:match('^@(.+)[/\\][^/\\]+$') or '.'

local function psLiteral(value)
    return "'" .. value:gsub("'", "''") .. "'"
end

local function encodedPowerShell(script)
    local bytes = {}
    local function unit(n)
        bytes[#bytes+1] = string.char(n%256, math.floor(n/256))
    end
    for _, cp in utf8.codes(script) do
        if cp < 65536 then unit(cp) else
            cp=cp-65536; unit(55296+math.floor(cp/1024)); unit(56320+cp%1024)
        end
    end
    local raw=table.concat(bytes)
    local alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    local output={}
    for i=1,#raw,3 do
        local a,b,c=raw:byte(i,i+2)
        local n=a*65536+(b or 0)*256+(c or 0)
        for shift=18,0,-6 do
            local index=math.floor(n/2^shift)%64+1
            output[#output+1]=alphabet:sub(index,index)
        end
        if not b then output[#output-1]='=' end
        if not c then output[#output]='=' end
    end
    return table.concat(output)
end

local installationDirectory = assert(pluginDirectory:gsub('\\','/'):match('^(.*)/share/xournalpp/plugins/[^/]+$'), '无法从插件目录确定 Xournal 安装目录')
local dataDirectory = installationDirectory .. '/PageOutline'
local cacheDirectory = dataDirectory .. '/cache'
local function ensureCacheDirectory()
    local probe=io.open(cacheDirectory..'/PageOutline-tree.tsv','ab')
    if probe then probe:close();return end
    local script="$ErrorActionPreference='Stop'; [void][IO.Directory]::CreateDirectory("..psLiteral(cacheDirectory)..')'
    assert(os.execute('powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand '..encodedPowerShell(script)), '无法创建大纲数据目录：'..cacheDirectory)
end

function outlineExportChosen(path)
    if not path or path == '' then return end
    guarded(function()
        if not path:lower():match('%.pdf$') then path=path..'.pdf' end
        assert(not path:find('[\r\n\t]'), '输出路径不能含换行或制表符。')
        local executable=pluginDirectory..'/PDF-Bookmarks.exe'
        local helper=io.open(executable,'rb')
        assert(helper, '缺少 PDF-Bookmarks.exe，请将新版工具和 main.lua 放在同一个插件文件夹。')
        helper:close()
        local headings=scan()
        assert(#headings>0, '没有标题。')
        local doc=app.getDocumentStructure()
        -- Lua's narrow Windows file APIs may interpret UTF-8 filenames as ANSI.
        -- Keep intermediate filenames independent of the chosen Unicode target;
        -- Python receives the final path as UTF-8 file content and uses wide APIs.
        ensureCacheDirectory()
        local temporary=cacheDirectory
        local raw=temporary..'/pageoutline-export-'..os.time()..'-'..math.random(100000,999999)..'.pdf'
        local manifest=raw..'.outline.tsv'
        local file=assert(io.open(manifest,'wb'))
        file:write('PageOutline-v1\t'..#doc.pages..'\n')
        for _, e in ipairs(headings) do
            local title=e.title:gsub('[\t\r\n]', ' ')
            file:write(string.format('%d\t%d\t%.6f\t%.6f\t%s\n',
                e.level,e.page,e.y,doc.pages[e.page].pageHeight,title))
        end
        file:close()
        -- Full document, one PDF page per notebook page; preserve background.
        app.export({outputFile=raw,background='all',progressiveMode=false})
        local job=raw..'.job.tsv'
        local request=assert(io.open(job,'wb'))
        request:write(raw..'\n'..path..'\n');request:close()
        -- All paths are PowerShell single-quoted literals inside UTF-16 Base64.
        -- cmd.exe only receives a fixed command and an ASCII Base64 token.
        local script="$ProgressPreference='SilentlyContinue'; $env:TEMP="..psLiteral(cacheDirectory).."; $env:TMP=$env:TEMP; $job=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("..psLiteral(job)..')); '
            ..'$process=Start-Process -FilePath '..psLiteral(executable)
            .." -ArgumentList @('--job-base64',$job) -WindowStyle Hidden -Wait -PassThru; exit $process.ExitCode"
        local success=os.execute('powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand '..encodedPowerShell(script))
        local result=io.open(job..'.result','rb')
        local message=result and result:read('*a') or '未收到书签工具结果，请检查工具是否被系统阻止。'
        if result then result:close() end
        message=message:gsub('\r\n','\n')
        if message:sub(1,7)=='CANCEL\n' then
            os.remove(raw);os.remove(manifest);os.remove(job);os.remove(job..'.result')
            notify('已取消覆盖，原 PDF 保留。');return
        end
        assert(success and message:sub(1,3)=='OK\n', 'PDF 书签生成失败：'..message..'\n原始导出保留在：'..raw)
        os.remove(raw);os.remove(manifest);os.remove(job);os.remove(job..'.result')
        notify('已导出带多级书签的 PDF：\n'..path..'\n可直接在 PDF 阅读器左侧书签栏中跳转。')
    end)
end

-- Windows companion bridge: the external tree asks this callback to run via
-- a registered accelerator. Lua has no portable background timer API.
local bridgeRoot = cacheDirectory .. '/PageOutline-'
local bridgeSession = tostring(os.time()) .. '-' .. tostring({}):gsub('[^%w]', '')
local bridgeEpoch, bridgeRows = 0, {}
local bridgeDocument,bridgePageCount

local function bridgeWrite(status, rows)
    bridgeRows = rows or scan()
    bridgeEpoch = bridgeEpoch + 1
    local doc = app.getDocumentStructure()
    local file = assert(io.open(bridgeRoot .. 'tree.tsv', 'wb'))
    file:write('PageOutlineTree-v2\t' .. bridgeSession .. '\t' .. bridgeEpoch .. '\t'..doc.currentPage..'\t'..#doc.pages..'\n')
    local name = doc.xoppFilename or doc.pdfBackgroundFilename or ''
    bridgeDocument=name;bridgePageCount=#doc.pages
    file:write((status or '已刷新'):gsub('[\r\n\t]', ' ') .. '\t' .. name:gsub('[\r\n\t]', ' ') .. '\n')
    for _, e in ipairs(bridgeRows) do
        file:write(string.format('%d\t%d\t%s\n',e.level,e.page,e.title:gsub('[\r\n\t]', ' ')))
    end
    file:close()
end

function outlineTreePublish()
    guarded(function()
        ensureCacheDirectory()
        local executable=pluginDirectory..'/Outline-Tree.exe'
        local file=io.open(executable,'rb')
        assert(file, '缺少 Outline-Tree.exe，请把它放在 main.lua 所在的插件目录。')
        file:close()
        local script="$ProgressPreference='SilentlyContinue'; $ErrorActionPreference='Stop'; $env:TEMP="..psLiteral(cacheDirectory).."; $env:TMP=$env:TEMP; "
            .."Start-Process -FilePath "..psLiteral(executable).." -WindowStyle Normal; exit 0"
        local success=os.execute('powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand '..encodedPowerShell(script))
        assert(success, '独立大纲启动失败，请检查程序是否被系统阻止。')
        bridgeWrite('已刷新')
    end)
end

local function changeHeadingLevel(chosen,level)
    requireApi({'setCurrentPage','setCurrentLayer','clearSelection','addToSelection','addTexts','getTexts','activateAction'})
    assert(chosen.layer and chosen.ref, '当前接口未返回标题的图层或引用，不能修改。')
    local doc=app.getDocumentStructure()
    local originalPage=doc.currentPage
    local originalLayer=doc.pages[originalPage].currentLayer
    local destinationLayer=doc.pages[chosen.page].currentLayer
    local created
    local ok,err=pcall(function()
        app.clearSelection()
        app.setCurrentPage(chosen.page);app.setCurrentLayer(chosen.layer,false)
        local target
        for _,box in ipairs(app.getTexts('layer')) do
            if box.ref==chosen.ref and box.text==chosen.text then target=box;break end
        end
        assert(target, '标题已变化，请刷新大纲后重试。')
        local text,count=target.text:gsub('^(%s*)#+(%s+)',function(prefix,space)
            return prefix..string.rep('#',level)..space
        end,1)
        assert(count==1, '没有找到标题前缀。')
        -- Create first: if insertion fails, the original text remains intact.
        created=app.addTexts({texts={{text=text,x=target.x,y=target.y,
            font=target.font,color=target.color,wrap=target.wrap}},allowUndoRedoAction='grouped'})
        assert(created and #created==1, '插入新标题失败。')
        app.clearSelection();app.addToSelection({target.ref})
        local selected=app.getTexts('selection')
        assert(#selected==1 and selected[1].ref==target.ref, '未能准确选中原标题。')
        app.activateAction('delete')
        for _,box in ipairs(app.getTexts('layer')) do
            assert(box.ref~=target.ref, '原标题未被删除，已取消替换。')
        end
        created=nil
        app.clearSelection()
    end)
    if not ok and created then
        -- Roll back only the replacement; do not touch the original on error.
        pcall(function()app.clearSelection();app.addToSelection(created);app.activateAction('delete');app.clearSelection()end)
    end
    pcall(function()
        app.setCurrentPage(chosen.page);app.setCurrentLayer(destinationLayer,false)
        app.setCurrentPage(originalPage);app.setCurrentLayer(originalLayer,false)
    end)
    if not ok then error(err) end
end

local function jumpCachedHeading(index)
    local chosen=bridgeRows[index]
    local doc=app.getDocumentStructure()
    local name=doc.xoppFilename or doc.pdfBackgroundFilename or ''
    if not chosen or name~=bridgeDocument or #doc.pages~=bridgePageCount then
        bridgeWrite('文档或页数已变化，请点击刷新后再跳转。',bridgeRows);return
    end
    requireApi({'setCurrentPage','setCurrentLayer','getTexts','scrollToPage'})
    local originalPage=doc.currentPage
    local targetPage=doc.pages[chosen.page]
    if not targetPage or not chosen.layer or not chosen.ref then
        bridgeWrite('标题定位信息不足，请刷新大纲。',bridgeRows);return
    end
    local targetLayer=targetPage.currentLayer
    local found=false
    local ok,err=pcall(function()
        -- Only visit the requested page, never scan the entire document on a jump.
        if originalPage~=chosen.page then app.setCurrentPage(chosen.page) end
        if targetLayer~=chosen.layer then app.setCurrentLayer(chosen.layer,false) end
        for _,box in ipairs(app.getTexts('layer')) do
            if box.ref==chosen.ref and box.text==chosen.text then found=true;break end
        end
    end)
    if targetLayer~=chosen.layer then pcall(app.setCurrentLayer,targetLayer,false) end
    if not ok or not found then
        if originalPage~=chosen.page then pcall(app.setCurrentPage,originalPage) end
        bridgeWrite('标题已变化，请点击刷新后再跳转。',bridgeRows);return
    end
    app.scrollToPage(chosen.page,false)
    bridgeWrite('已跳转：'..chosen.title..'（第'..chosen.page..'页）',bridgeRows)
end

function outlineTreeBridge()
    guarded(function()
        local file = io.open(bridgeRoot .. 'command.tsv', 'rb')
        if not file then bridgeWrite('已刷新'); return end
        local line = file:read('*l') or ''; file:close()
        os.remove(bridgeRoot .. 'command.tsv')
        local session,epoch,action,index,payload=line:match('^([^\t]+)\t(%d+)\t([^\t]+)\t(%d+)\t?(.*)$')
        if action == 'refresh' then bridgeWrite('已刷新'); return end
        if session ~= bridgeSession or tonumber(epoch) ~= bridgeEpoch then
            bridgeWrite('数据已变化，请重新点击标题。',bridgeRows); return
        end
        if action=='info' then
            local doc=app.getDocumentStructure()
            local name=doc.xoppFilename or doc.pdfBackgroundFilename or ''
            bridgeWrite('当前位置已更新',name==bridgeDocument and bridgeRows or {});return
        end
        if action=='open' then
            requireApi({'openFile'})
            local path=payload:gsub('%%(%x%x)',function(hex)return string.char(tonumber(hex,16))end)
            assert(path:lower():match('%.xopp$') and not path:find('[\r\n\t]'),'笔记路径无效。')
            local doc=app.getDocumentStructure()
            local function normalized(v)return (v or ''):gsub('\\','/'):lower()end
            if normalized(doc.xoppFilename)==normalized(path) then
                local page=tonumber(index)
                assert(page>=1 and page<=#doc.pages,'书签页码超出范围，请更新索引或书签。')
                app.setCurrentPage(page);app.scrollToPage(page,false)
                bridgeWrite('已跳转到第'..page..'页',bridgeRows)
            else
                -- The API is asynchronous: never suppress its unsaved-note prompt.
                app.openFile(path,math.max(1,tonumber(index)),false)
                bridgeWrite('已发送打开请求；完成保存确认后请点击刷新。',{})
            end
            return
        end
        if action=='jump' then jumpCachedHeading(tonumber(index));return end
        local current = scan()
        local unchanged = #current == #bridgeRows
        for i, e in ipairs(current) do
            local old=bridgeRows[i]
            if not old or e.page~=old.page or e.title~=old.title or e.y~=old.y or e.level~=old.level then unchanged=false end
        end
        local chosen=current[tonumber(index)]
        local level=action:match('^level([123])$')
        if (action ~= 'jump' and not level) or not unchanged or not chosen then
            bridgeWrite('标题或页面已变化，请重新点击标题。', current); return
        end
        if level then
            level=tonumber(level)
            if chosen.level==level then bridgeWrite('该标题已经是'..level..'级。',current);return end
            changeHeadingLevel(chosen,level)
            bridgeWrite('已将“'..chosen.title..'”改为'..level..'级，请保存笔记。')
            return
        end
        requireApi({'scrollToPage'})
        if type(app.setCurrentPage)=='function' then app.setCurrentPage(chosen.page) end
        app.scrollToPage(chosen.page,false)
        bridgeWrite('已跳转：' .. chosen.title .. '（第' .. chosen.page .. '页）', current)
    end)
end

function initUi()
    for level=1,3 do
        app.registerUi({menu='插入'..level..'级标题模板',callback='outlineAdd',mode=level})
    end
    app.registerUi({menu='在当前页插入目录文字',callback='outlineInsertToc'})
    app.registerUi({menu='导出带大纲的 PDF',callback='outlineExportPdf'})
    app.registerUi({menu='打开/刷新独立大纲窗口',callback='outlineTreePublish',accelerator='<Control><Shift>F8'})
    app.registerUi({menu='独立大纲通信',callback='outlineTreeBridge',accelerator='<Control><Alt><Shift>F12'})
end
