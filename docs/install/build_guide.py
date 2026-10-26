"""Build a shareable Russian PDF using real, secret-free installer screenshots."""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'Установка Telegram AI Sender.pdf'
pdfmetrics.registerFont(TTFont('Arial',r'C:\Windows\Fonts\arial.ttf'))
pdfmetrics.registerFont(TTFont('ArialBold',r'C:\Windows\Fonts\arialbd.ttf'))
W,H=A4;M=43;WIDTH=W-2*M
body=ParagraphStyle('body',fontName='Arial',fontSize=11,leading=16,textColor='#182434',spaceAfter=8)
small=ParagraphStyle('small',parent=body,fontSize=9,leading=13,textColor='#556170')
title=ParagraphStyle('title',parent=body,fontName='ArialBold',fontSize=25,leading=31,textColor='#000000')
heading=ParagraphStyle('heading',parent=body,fontName='ArialBold',fontSize=16,leading=21,textColor='#000000')
c=canvas.Canvas(str(OUT),pagesize=A4);c.setTitle('Установка Telegram AI Sender');c.setAuthor('ДВК')
page=0;y=0
def new(label):
    global page,y
    if page:c.showPage()
    page+=1;y=H-45
    c.setFont('Arial',9);c.setFillColorRGB(.35,.40,.47);c.drawString(M,24,'ДВК · Первая установка · Windows / Mac / Linux')
    c.drawRightString(W-M,24,str(page))
    text(label,title);gap(10)
def text(value,style=body):
    global y
    p=Paragraph(value,style);_,h=p.wrap(WIDTH,1000)
    if y-h<45:raise ValueError(f'Page {page} overflow: {value[:40]}')
    p.drawOn(c,M,y-h);y-=h+style.spaceAfter
def gap(size):
    global y;y-=size
def shot(name,height=None):
    global y
    img=ImageReader(str(ROOT/'images'/name));iw,ih=img.getSize();h=WIDTH*ih/iw
    if height and h>height:w=WIDTH*height/h;h=height
    else:w=WIDTH
    if y-h<45:raise ValueError('Image overflow')
    c.drawImage(img,M+(WIDTH-w)/2,y-h,width=w,height=h);y-=h+10
def step(n,value):text('<b>'+str(n)+'.</b> '+value)
def link(label,url):return '<link href="'+url+'" color="#235fc7">'+label+'</link>'
BASE='https://github.com/popetrov/telegram-ai-sender-dvk/releases/latest/download/'

new('Установка Telegram AI Sender')
text('Инструкция для тех, кто устанавливает программу впервые. После настройки вы будете открывать один файл запуска; новые версии программа проверяет сама.')
text('Перед началом',heading)
text('Подготовьте компьютер с интернетом и телефон с вашим Telegram. У руководителя получите номер менеджера и ключ ИИ. Для продуктов ДВК Право нужен также ключ судебного поиска. Параметры Telegram-приложения загрузятся автоматически после разрешения центра.')
text('Токен своего личного бота вы создадите самостоятельно по следующей странице. Номер менеджера не придумывайте: если выдан номер <b>010</b>, вводите именно <b>010</b>, с нулями.')
text('Скачайте один архив для своей системы',heading)
text('<b>Windows:</b> '+link('DVK_INSTALL_WINDOWS.zip',BASE+'DVK_INSTALL_WINDOWS.zip'))
text('<b>Mac:</b> '+link('DVK_INSTALL_MACOS.zip',BASE+'DVK_INSTALL_MACOS.zip'))
text('<b>Linux:</b> '+link('DVK_INSTALL_LINUX.zip',BASE+'DVK_INSTALL_LINUX.zip'))
text('Нужен именно архив <b>DVK_INSTALL</b>. Файлы с названием telegram_ai_sender_v… предназначены для обновления старых установок.')
text('Порядок действий',heading)
step(1,'Создайте бота и скопируйте токен — страница 2.')
step(2,'Распакуйте архив и запустите установку — страница 3.')
step(3,'Заполните страницу настройки — страница 4.')
step(4,'Войдите в Telegram по QR-коду и дождитесь доступа — страница 5.')
text('Python вручную искать не нужно. Если на компьютере уже есть подходящая 64-разрядная версия Python 3.11–3.14, она используется повторно. Иначе установщик подготовит совместимую версию.',small)

new('Создайте своего бота')
text('Откройте '+link('@BotFather','https://t.me/BotFather')+' в Telegram. Проверьте точное имя <b>@BotFather</b> и отметку официального аккаунта.')
shot('botfather.png',225)
step(1,'Нажмите <b>Запустить / Start</b>. Отправьте команду <b>/newbot</b>.')
step(2,'Введите название, например <b>Помощник Виктории</b>.')
step(3,'Введите свободный username на латинице с окончанием <b>bot</b>, например <b>viktoria_dvk_010_bot</b>. Если имя занято, выберите другое.')
step(4,'В ответе BotFather найдите токен. Это одна строка вида <b>цифры:буквы_и_символы</b>. Скопируйте только её, без текста вокруг.')
step(5,'Откройте ссылку на созданного бота и нажмите <b>Запустить</b>. Пока программа не работает на компьютере, бот может не отвечать — это нормально.')
text('Токен — пароль управления ботом. Вставляйте его только в свою настройку программы. Не пересылайте токен или ключи в общий чат и не показывайте их на снимках экрана.',small)

new('Установите программу')
text('Выберите раздел для своего компьютера. Во время установки оставьте интернет включённым. Не закрывайте окно, пока программа готовит компоненты.')
text('Windows',heading)
step(1,'Нажмите по ZIP правой кнопкой → <b>Извлечь всё</b>. Выберите постоянную папку, например <b>Документы → DVK Sender</b>.')
step(2,'Откройте распакованную папку. Дважды нажмите <b>install.bat</b>. Из открытого ZIP запускать нельзя.')
step(3,'Дождитесь проверки или установки Python и компонентов. Откроется страница настройки в браузере — переходите к странице 4.')
text('Нужна 64-разрядная Windows 10/11. Если система блокирует запуск, передайте руководителю снимок сообщения; защиту компьютера отключать не нужно.',small)
text('Mac',heading)
step(1,'Распакуйте ZIP двойным нажатием. Перенесите полученную папку в <b>Документы</b>.')
step(2,'Откройте <b>install_mac.command</b>. Если нужен Python, установщик скачает официальный подписанный пакет.')
step(3,'Если запрошен пароль администратора Mac, введите его в Терминале. Символы пароля не отображаются. Дождитесь страницы настройки.')
text('Если файл открылся как текст: откройте Терминал, введите <b>bash</b> и пробел, перетащите файл install_mac.command в Терминал и нажмите Enter. При системном запрете открытия или отсутствии прав обратитесь к руководителю.',small)
text('Linux',heading)
step(1,'Распакуйте ZIP в постоянную папку. Откройте в ней Терминал.')
step(2,'Введите <b>bash install_linux.sh</b> и нажмите Enter. При запросе введите пароль администратора.')
step(3,'Дождитесь страницы настройки. На старой системе подготовка Python может занять больше времени.')
text('Автоматическая установка системных компонентов предусмотрена для Ubuntu/Debian, Fedora и Arch с графическим рабочим столом. На другом дистрибутиве подготовку Python выполняет руководитель или специалист.',small)

new('Заполните настройку')
text('Страница открывается в браузере автоматически. Она работает на вашем компьютере и сохраняет настройки локально. На снимке показаны вымышленные данные.')
shot('setup.png')
text('Что ввести',heading)
text('<b>Номер менеджера:</b> номер от руководителя, с ведущими нулями.<br/><b>Токен бота:</b> строка, полученная на странице 2.<br/><b>Ключ ИИ:</b> получите у владельца программы или руководителя компании.<br/><b>Ключ судебного поиска:</b> получите у руководителя, если работаете с ДВК Право. API ID и API Hash вручную вводить не нужно.')
text('Звёздочки или точки в полях токенов и ключей — нормальная защита отображения. Для вставки используйте Ctrl+V на Windows/Linux или Command+V на Mac.',small)

new('Войдите и получите доступ')
text('Внизу страницы нажмите <b>Сохранить и запустить</b>. Настройки второй раз вводить не потребуется.')
shot('setup-save.png',270)
step(1,'Откроется технический бот центра. Нажмите <b>Запустить</b> и дождитесь разрешения руководителя. Вернитесь в программу: появится QR-код. Для входа используйте тот же Telegram-аккаунт.')
step(2,'На телефоне откройте Telegram → <b>Настройки → Устройства → Подключить устройство</b>. Отсканируйте QR-код на своём компьютере.')
step(3,'Если включён облачный пароль Telegram, введите его в программе. QR-код и пароль никому не пересылайте.')
step(4,'Откройте своего бота в Telegram и нажмите <b>Запустить</b>. Оставьте программу на компьютере работающей.')
step(5,'Дождитесь разрешения руководителя. Проверяйте кнопкой <b>Доступ</b> или командой <b>/access</b>. Подтверждение может появиться через несколько минут.')
text('До разрешения центра отправка клиентам недоступна. Установка и вход в Telegram не запускают рассылку автоматически.',small)

new('Как запускать дальше')
text('Открывайте файл из той же папки, которую выбрали при установке. Переустанавливать программу для каждого обновления не нужно.')
text('<b>Windows:</b> дважды нажмите <b>start.bat</b>.<br/><b>Mac:</b> откройте <b>start_mac.command</b>.<br/><b>Linux:</b> откройте Терминал в папке программы и выполните <b>bash start_linux.sh</b>.')
shot('setup-done.png',220)
text('Во время работы',heading)
text('Оставляйте программу запущенной, компьютер включённым и подключённым к интернету. Управляйте через кнопки своего Telegram-бота. Отправка начинается после подготовки списка и вашего подтверждения.')
text('При перезапуске проверяются обновления. Вход, номер и локальная база сохраняются. Не удаляйте папки программы и не переносите отдельные файлы базы или сессии между компьютерами.')
text('Если появилась ошибка',heading)
text('Не переустанавливайте всё заново. Отправьте руководителю название системы, этап остановки и снимок ошибки без ключей, токенов и QR-кода. Если ключей ещё нет, получите их у руководителя до заполнения настройки.')
text('Источники: '+link('создание бота','https://core.telegram.org/bots/tutorial#obtain-your-bot-token')+' · '+link('официальный Python','https://www.python.org/downloads/')+'. Скриншоты настройки сняты на демонстрации без действующих ключей. Редакция от 26 октября 2026 года.',small)
c.save();print(OUT)
