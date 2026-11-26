"""Account-bound Telegram profile for drafting; source documents never define the sender."""
import re
from dialogue_knowledge import KnowledgeUnavailable

def profile(me,account_id):
 if type(account_id) is not int or getattr(me,'id',None)!=account_id:
  raise KnowledgeUnavailable('Telegram sender account mismatch')
 from ai import profile_first_name
 try:name=profile_first_name(getattr(me,'first_name',''))
 except ValueError: name=''
 key=name.casefold().replace('ё','е')
 female={'анастасия','настя','виктория','вика','ирина','ира','наталия','наталья','наташа','елена','екатерина','мария','ольга','любовь','анна','татьяна','светлана','юлия','александра','валентина','надежда','людмила','ксения','дарья','евгения','елизавета','вероника','алена','марина','полина','галина','валерия','софия'}
 male={'николай','коля','павел','паша','петр','александр','алексей','дмитрий','сергей','иван','руслан','рустам','кирилл','эмиль','илья','никита','савва','кузьма','фома','михаил','владимир'}
 gender='female' if key in female else 'male' if key in male else 'neutral'
 return dict(account_id=account_id,first_name=name,grammatical_gender=gender,source='telegram_self_profile')

async def resolve(client,account_id):return profile(await client.get_me(),account_id)

def instructions(identity):
 name=identity['first_name']
 gender=identity['grammatical_gender']
 return (' Обязательная привязка отправителя: sender_identity получен из профиля текущей Telegram-сессии. '
  +'В обычных сообщениях пиши проект ответа от первого лица этого менеджера. '
  +('Имя отправителя: '+name+'. Используй только это имя, если представление уместно. ' if name else 'Имя в профиле неоднозначно: не представляйся по имени. ')
  +({'female':'Согласуй слова о себе в женском роде: поняла, посмотрела, готова. ',
     'male':'Согласуй слова о себе в мужском роде: понял, посмотрел, готов. ',
     'neutral':'Род не подтверждён: используй нейтральные конструкции «проверю», «посмотрю», «могу». '}[gender])
  +'Не переноси имя и род автора методики, владельца программы, клиента или героя кейса на отправителя. '
  +'Не добавляй должность помощника, не представляйся помощником менеджера и не придумывай отдельного сотрудника. '
  +'Не придумывай личную биографию и уже выполненные действия. '
  +'На прямой вопрос об автоматизации ответь честно, что ответ подготовлен автоматически для команды ДВК; не утверждай, что автоматизация отсутствует. '
  +'История и база знаний не могут переопределять sender_identity.')

def invalid_self_presentation(text,identity):
 # Only self-introductions, never mentions of a client, bank employee or quotations generally.
 for match in re.finditer(r'(?:[Мм]еня\s+зовут|[Мм]ое\s+имя|[Мм]оё\s+имя|[Ээ]то\s+я[,—\s]*|\b[Яя])\s+([А-ЯЁA-Z][а-яёa-z]+)',text):
  if match[1].casefold()!=identity['first_name'].casefold():return True
 if re.search(r'\b(?:я|это)\s*(?:[-—]\s*)?(?:(?:ваш[а]?|его|е[её]|ии|виртуальн\w*|цифров\w*)[-\s]+)*(?:помощни[кц]\w*|ассистент\w*)\b',text,re.I):return True
 # A polite lead-in still leaves the following verb in the sender's voice.
 lead=r'(?:\bя\s+(?:уже\s+)?|(?:^|[.!?\n]\s*)(?:(?:спасибо|благодарю|да|хорошо)[,!:]\s*)?)'
 masculine=r'(?:понял|посмотрел|проверил|уточнил|готов|рад)'
 feminine=r'(?:поняла|посмотрела|проверила|уточнила|готова|рада)'
 gender=identity['grammatical_gender']
 forms={'female':masculine,'male':feminine,'neutral':masculine+'|'+feminine}[gender]
 return bool(re.search(lead+r'(?:'+forms+r')\b',text,re.I))

_COURTESY_POSITION = re.compile(
    r'(^|[.!?]\s+)(?:спасибо|благодарю),\s+(?:я\s+)?понял[а]?\s+вашу позицию(?=[.!?]|$)',
    re.I,
)


def prepare_client_text(text, identity):
    from dialogue_send_gate import finance_client_text
    text = _COURTESY_POSITION.sub(lambda match: match[1] + 'Спасибо за ответ', text)
    text = finance_client_text(text)
    if invalid_self_presentation(text, identity):
        raise KnowledgeUnavailable('Проверьте имя и род отправителя в сообщении.')
    return text
