# app.py - Complete Indian WhatsApp Personal Assistant
import os
import json
import pytz
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse
from googletrans import Translator
from apscheduler.schedulers.background import BackgroundScheduler
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
import redis
from dotenv import load_dotenv
import speech_recognition as sr
import tempfile
import re

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize services
translator = Translator()
scheduler = BackgroundScheduler()
scheduler.start()

# Redis for session management
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'localhost'),
    port=int(os.getenv('REDIS_PORT', 6379)),
    decode_responses=True
)

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')
TWILIO_VOICE_NUMBER = os.getenv('TWILIO_VOICE_NUMBER')

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Google Calendar API scopes
SCOPES = ['https://www.googleapis.com/auth/calendar']  # Full access for read/write

# Language configurations
LANGUAGES = {
    'hi': {
        'name': 'Hindi',
        'voice': 'Polly.Aditi',
        'code': 'hi-IN'
    },
    'en': {
        'name': 'English',
        'voice': 'Polly.Raveena',
        'code': 'en-IN'
    }
}

# Message templates
MESSAGES = {
    'welcome': {
        'hi': '🙏 नमस्ते! मैं आपका व्यक्तिगत सहायक हूं। मैं आपकी मदद कर सकता हूं:\n\n'
              '📅 कैलेंडर और रिमाइंडर\n'
              '🍳 रेसिपी खोजना\n'
              '📞 कॉल रिमाइंडर\n'
              '💡 सुझाव देना\n\n'
              'कोशिश करें: "कल सुबह 7 बजे याद दिलाना" या "पनीर की रेसिपी"\n\n'
              '✨ मुझे कोई नाम देना चाहते हैं? बस लिखें "तुम्हारा नाम [नाम] है"',
        'en': '🙏 Hello! I\'m your personal assistant. I can help you with:\n\n'
              '📅 Calendar and reminders\n'
              '🍳 Finding recipes\n'
              '📞 Call reminders\n'
              '💡 Suggestions\n\n'
              'Try: "Remind me tomorrow at 7 AM" or "Paneer recipe"\n\n'
              '✨ Want to give me a name? Just say "Your name is [name]"'
    },
    'name_set': {
        'hi': '😊 धन्यवाद! अब से मेरा नाम {name} है। आप मुझे {name} कह सकते हैं!',
        'en': '😊 Thank you! From now on, my name is {name}. You can call me {name}!'
    },
    'introduction': {
        'hi': '👋 नमस्ते! मैं {name} हूं, आपका व्यक्तिगत सहायक। कैसे मदद कर सकता हूं?',
        'en': '👋 Hello! I\'m {name}, your personal assistant. How can I help you?'
    },
    'reminder_set': {
        'hi': '✅ रिमाइंडर सेट: {task}\n📅 {date}\n⏰ {time}',
        'en': '✅ Reminder set: {task}\n📅 {date}\n⏰ {time}'
    },
    'morning_greeting': {
        'hi': '🌅 शुभ प्रभात! आज {date} है\n\n📋 आज का कार्यक्रम:\n{schedule}\n\n💭 विचार: {quote}',
        'en': '🌅 Good morning! Today is {date}\n\n📋 Today\'s schedule:\n{schedule}\n\n💭 Thought: {quote}'
    },
    'recipe_found': {
        'hi': '🍳 {dish} बनाने की विधि:\n\n📝 सामग्री:\n{ingredients}\n\n👨‍🍳 विधि:\n{method}\n\n⏱️ समय: {time}',
        'en': '🍳 Recipe for {dish}:\n\n📝 Ingredients:\n{ingredients}\n\n👨‍🍳 Method:\n{method}\n\n⏱️ Time: {time}'
    },
    'proactive_morning': {
        'hi': '🌅 शुभ प्रभात! मैं {assistant_name} हूं।\n\n आज के लिए क्या प्लान है? मुझे बताएं अगर कोई रिमाइंडर चाहिए! 😊\n\n💡 टिप: आप वॉइस नोट भी भेज सकते हैं!',
        'en': '🌅 Good morning! It\'s {assistant_name} here.\n\n What are your plans for today? Let me know if you need any reminders! 😊\n\n💡 Tip: You can also send me voice notes!'
    },
    'proactive_afternoon': {
        'hi': '☀️ नमस्ते! {assistant_name} यहाँ है।\n\n दिन कैसा जा रहा है? कोई रिमाइंडर या मदद चाहिए? 🤔',
        'en': '☀️ Hello! {assistant_name} checking in.\n\n How\'s your day going? Need any reminders or help? 🤔'
    },
    'proactive_evening': {
        'hi': '🌆 शाम की चाय का समय! ☕\n\n कल के लिए कुछ प्लान करना है? मैं {assistant_name}, मदद के लिए तैयार हूं!',
        'en': '🌆 Evening tea time! ☕\n\n Want to plan anything for tomorrow? {assistant_name} here to help!'
    },
    'voice_received': {
        'hi': '🎤 वॉइस नोट मिला! मैं इसे सुन रहा हूं...',
        'en': '🎤 Voice note received! Let me listen to this...'
    },
    'voice_processed': {
        'hi': '✅ समझ गया! मैंने ये रिमाइंडर सेट किए हैं:\n{reminders}\n\n कुछ और जोड़ना है?',
        'en': '✅ Got it! I\'ve set these reminders:\n{reminders}\n\n Anything else to add?'
    },
    'calendar_add': {
        'hi': '✅ कैलेंडर में जोड़ा गया:\n📅 {title}\n⏰ {date} को {time}\n⏱️ अवधि: {duration} मिनट\n🔗 {link}',
        'en': '✅ Added to calendar:\n📅 {title}\n⏰ {date} at {time}\n⏱️ Duration: {duration} minutes\n🔗 {link}'
    },
    'calendar_error': {
        'hi': '❌ कैलेंडर में जोड़ने में त्रुटि। कृपया फिर से कोशिश करें।\nउदाहरण: "कल 3 बजे मीटिंग कैलेंडर में जोड़ें"',
        'en': '❌ Error adding to calendar. Please try again.\nExample: "Add meeting tomorrow at 3 PM to calendar"'
    },
}

# Indian recipes database
RECIPES = {
    'paneer': {
        'hi': {
            'name': 'पनीर बटर मसाला',
            'ingredients': '• 250g पनीर\n• 2 प्याज\n• 3 टमाटर\n• 1/2 कप क्रीम\n• मसाले',
            'method': '1. प्याज-टमाटर का पेस्ट बनाएं\n2. मसाले भूनें\n3. पेस्ट डालें\n4. क्रीम और पनीर मिलाएं\n5. 5 मिनट पकाएं',
            'time': '30 मिनट'
        },
        'en': {
            'name': 'Paneer Butter Masala',
            'ingredients': '• 250g paneer\n• 2 onions\n• 3 tomatoes\n• 1/2 cup cream\n• Spices',
            'method': '1. Make onion-tomato paste\n2. Sauté spices\n3. Add paste\n4. Mix cream and paneer\n5. Cook for 5 mins',
            'time': '30 minutes'
        }
    },
    'dal': {
        'hi': {
            'name': 'दाल तड़का',
            'ingredients': '• 1 कप अरहर दाल\n• 1 प्याज\n• 2 टमाटर\n• तड़का मसाले',
            'method': '1. दाल उबालें\n2. तड़का तैयार करें\n3. प्याज-टमाटर भूनें\n4. दाल मिलाएं\n5. 10 मिनट पकाएं',
            'time': '45 मिनट'
        },
        'en': {
            'name': 'Dal Tadka',
            'ingredients': '• 1 cup toor dal\n• 1 onion\n• 2 tomatoes\n• Tempering spices',
            'method': '1. Boil dal\n2. Prepare tempering\n3. Sauté onion-tomato\n4. Mix dal\n5. Cook for 10 mins',
            'time': '45 minutes'
        }
    }
}

# Motivational quotes
QUOTES = {
    'hi': [
        'जो आज कठिन लग रहा है, वह कल आपकी ताकत बनेगा।',
        'सफलता की शुरुआत हमेशा छोटे कदमों से होती है।',
        'हर नया दिन एक नई शुरुआत है।'
    ],
    'en': [
        'What seems difficult today will become your strength tomorrow.',
        'Success always begins with small steps.',
        'Every new day is a fresh start.'
    ]
}

class UserSession:
    """Manage user sessions and preferences"""
    
    def __init__(self, phone_number):
        self.phone_number = phone_number
        self.key = f"user:{phone_number}"
    
    def get_data(self):
        data = redis_client.hgetall(self.key)
        if not data:
            # Initialize new user
            data = {
                'language': 'en',
                'timezone': 'Asia/Kolkata',
                'name': 'Friend',
                'assistant_name': 'Assistant',
                'created_at': datetime.now().isoformat()
            }
            self.save_data(data)
        return data
    
    def save_data(self, data):
        redis_client.hset(self.key, mapping=data)
    
    def get_language(self):
        return self.get_data().get('language', 'en')
    
    def set_language(self, lang):
        data = self.get_data()
        data['language'] = lang
        self.save_data(data)
    
    def get_assistant_name(self):
        return self.get_data().get('assistant_name', 'Assistant')
    
    def set_assistant_name(self, name):
        data = self.get_data()
        data['assistant_name'] = name
        self.save_data(data)

class CalendarService:
    """Google Calendar integration"""
    
    @staticmethod
    def get_credentials():
        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # In production, implement OAuth flow
                pass
        
        return creds
    
    @staticmethod
    def get_today_events(user_email=None):
        """Get today's calendar events"""
        try:
            creds = CalendarService.get_credentials()
            if not creds:
                return []
            
            service = build('calendar', 'v3', credentials=creds)
            
            # Get today's date range in IST
            ist = pytz.timezone('Asia/Kolkata')
            today_start = datetime.now(ist).replace(hour=0, minute=0, second=0)
            today_end = today_start + timedelta(days=1)
            
            events_result = service.events().list(
                calendarId='primary',
                timeMin=today_start.isoformat(),
                timeMax=today_end.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            return events
        except Exception as e:
            logger.error(f"Calendar error: {e}")
            return []
    
    @staticmethod
    def create_event(title, date_time, duration_minutes=60, description=None, location=None):
        """Create a new calendar event"""
        try:
            creds = CalendarService.get_credentials()
            if not creds:
                return {'success': False, 'error': 'No credentials'}
            
            service = build('calendar', 'v3', credentials=creds)
            
            # Create event body
            event = {
                'summary': title,
                'start': {
                    'dateTime': date_time.isoformat(),
                    'timeZone': 'Asia/Kolkata',
                },
                'end': {
                    'dateTime': (date_time + timedelta(minutes=duration_minutes)).isoformat(),
                    'timeZone': 'Asia/Kolkata',
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': 10},
                    ],
                },
            }
            
            if description:
                event['description'] = description
            if location:
                event['location'] = location
            
            # Insert event
            created_event = service.events().insert(calendarId='primary', body=event).execute()
            
            return {
                'success': True,
                'event_id': created_event.get('id'),
                'link': created_event.get('htmlLink')
            }
            
        except Exception as e:
            logger.error(f"Error creating event: {e}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    def parse_calendar_command(text, lang='en'):
        """Parse calendar add command"""
        import re
        from dateutil import parser as date_parser
        
        # Extract event title
        title_patterns = {
            'en': [
                r'add (.+?) to my calendar',
                r'schedule (.+?) for',
                r'calendar (.+?) at',
                r'meeting about (.+?) on'
            ],
            'hi': [
                r'कैलेंडर में (.+?) जोड़',
                r'(.+?) के लिए समय',
                r'(.+?) की मीटिंग'
            ]
        }
        
        title = None
        for pattern in title_patterns.get(lang, title_patterns['en']):
            match = re.search(pattern, text.lower())
            if match:
                title = match.group(1).strip()
                break
        
        if not title:
            # Try to extract title differently
            # Remove common words and extract the main subject
            remove_words = ['add', 'calendar', 'schedule', 'meeting', 'tomorrow', 'today', 'at', 'on', 'for']
            words = text.lower().split()
            title_words = [w for w in words if w not in remove_words and not w.isdigit()]
            title = ' '.join(title_words[:5])  # Take first 5 meaningful words
        
        # Extract date and time
        try:
            # Look for time patterns
            time_match = re.search(r'(\d{1,2})\s*(am|pm|AM|PM|बजे)', text)
            date_match = re.search(r'(tomorrow|कल|today|आज|monday|tuesday|wednesday|thursday|friday|saturday|sunday)', text.lower())
            
            ist = pytz.timezone('Asia/Kolkata')
            event_time = datetime.now(ist)
            
            # Parse date
            if date_match:
                date_word = date_match.group(1)
                if date_word in ['tomorrow', 'कल']:
                    event_time += timedelta(days=1)
                elif date_word in ['today', 'आज']:
                    pass  # Keep current date
                else:
                    # Day of week
                    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                    if date_word in days:
                        target_day = days.index(date_word)
                        current_day = event_time.weekday()
                        days_ahead = (target_day - current_day) % 7
                        if days_ahead == 0:
                            days_ahead = 7  # Next week
                        event_time += timedelta(days=days_ahead)
            
            # Parse time
            if time_match:
                hour = int(time_match.group(1))
                period = time_match.group(2).lower()
                if period in ['pm', 'बजे'] and hour != 12:
                    hour += 12
                elif period == 'am' and hour == 12:
                    hour = 0
                event_time = event_time.replace(hour=hour, minute=0, second=0)
            else:
                # Default to 9 AM if no time specified
                event_time = event_time.replace(hour=9, minute=0, second=0)
            
            # Extract duration (optional)
            duration = 60  # Default 1 hour
            duration_match = re.search(r'(\d+)\s*(hour|hr|घंटे|minute|min|मिनट)', text.lower())
            if duration_match:
                dur_value = int(duration_match.group(1))
                dur_unit = duration_match.group(2)
                if dur_unit in ['hour', 'hr', 'घंटे']:
                    duration = dur_value * 60
                else:
                    duration = dur_value
            
            return {
                'success': True,
                'title': title.title(),
                'datetime': event_time,
                'duration': duration
            }
            
        except Exception as e:
            logger.error(f"Error parsing calendar command: {e}")
            return {'success': False}

class ReminderService:
    """Handle reminders and scheduling"""
    
    @staticmethod
    def parse_reminder(text, lang='en'):
        """Parse reminder text to extract time and task"""
        # Simple parsing - in production, use NLP
        import re
        
        # Time patterns
        time_patterns = {
            'hi': r'(\d{1,2})\s*बजे',
            'en': r'(\d{1,2})\s*(am|pm|AM|PM)'
        }
        
        # Extract time
        time_match = re.search(time_patterns.get(lang, time_patterns['en']), text)
        if time_match:
            hour = int(time_match.group(1))
            if lang == 'en' and time_match.group(2).lower() == 'pm' and hour != 12:
                hour += 12
            
            # Extract date (tomorrow, today, etc.)
            tomorrow_words = {'tomorrow', 'कल', 'kal'}
            is_tomorrow = any(word in text.lower() for word in tomorrow_words)
            
            # Create reminder time
            ist = pytz.timezone('Asia/Kolkata')
            reminder_time = datetime.now(ist).replace(hour=hour, minute=0, second=0)
            if is_tomorrow:
                reminder_time += timedelta(days=1)
            
            # Extract task (remove time-related words)
            task = text
            for word in ['remind', 'याद', 'बजे', 'am', 'pm', 'tomorrow', 'कल']:
                task = task.replace(word, '')
            task = ' '.join(task.split())
            
            return {
                'task': task,
                'time': reminder_time,
                'success': True
            }
        
        return {'success': False}
    
    @staticmethod
    def schedule_reminder(phone_number, task, reminder_time, lang='en'):
        """Schedule a reminder"""
        job_id = f"reminder_{phone_number}_{reminder_time.timestamp()}"
        
        scheduler.add_job(
            func=send_reminder,
            trigger='date',
            run_date=reminder_time,
            args=[phone_number, task, lang],
            id=job_id
        )
        
        # Store in Redis
        reminder_key = f"reminder:{phone_number}:{job_id}"
        redis_client.hset(reminder_key, mapping={
            'task': task,
            'time': reminder_time.isoformat(),
            'lang': lang
        })
        
        return job_id

def send_reminder(phone_number, task, lang='en'):
    """Send reminder via WhatsApp and optionally call"""
    # Get assistant name
    assistant_name = UserSession(phone_number).get_assistant_name()
    
    if lang == 'hi':
        message = f"🔔 {assistant_name} की ओर से रिमाइंडर: {task}"
    else:
        message = f"🔔 Reminder from {assistant_name}: {task}"
    
    # Send WhatsApp message
    twilio_client.messages.create(
        body=message,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=phone_number
    )
    
    # Optional: Make a call for important reminders
    if 'medicine' in task.lower() or 'दवा' in task:
        make_reminder_call(phone_number, task, lang)

def make_reminder_call(phone_number, task, lang='en'):
    """Make a voice call reminder"""
    # Create TwiML for the call
    response = VoiceResponse()
    
    # Use Polly for Indian language support with assistant's name
    voice = LANGUAGES[lang]['voice']
    assistant_name = UserSession(phone_number).get_assistant_name()
    
    if lang == 'hi':
        message = f"नमस्ते, मैं {assistant_name} हूं। यह आपका रिमाइंडर है: {task}"
    else:
        message = f"Hello, this is {assistant_name}. This is your reminder: {task}"
    
    response.say(message, voice=voice, language=LANGUAGES[lang]['code'])
    response.pause(length=1)
    response.say("Press 1 to confirm, or 2 to snooze for 10 minutes", 
                 voice=voice, language=LANGUAGES[lang]['code'])
    response.gather(numDigits=1, action='/handle-reminder-response', method='POST')
    
    # Make the call
    call = twilio_client.calls.create(
        twiml=str(response),
        to=phone_number.replace('whatsapp:', ''),
        from_=TWILIO_VOICE_NUMBER
    )
    
    return call.sid

def detect_intent(text, lang='en'):
    """Detect user intent from message"""
    text_lower = text.lower()
    
    # Intent patterns
    intents = {
        'greeting': {
            'hi': ['नमस्ते', 'हेलो', 'हाय', 'हैलो'],
            'en': ['hello', 'hi', 'hey', 'namaste']
        },
        'set_name': {
            'hi': ['तुम्हारा नाम', 'आपका नाम', 'नाम है'],
            'en': ['your name is', 'call you', 'name you']
        },
        'reminder': {
            'hi': ['याद', 'रिमाइंडर', 'बजे', 'कल'],
            'en': ['remind', 'reminder', 'tomorrow', 'alarm']
        },
        'schedule': {
            'hi': ['कार्यक्रम', 'आज', 'कैलेंडर', 'शेड्यूल'],
            'en': ['schedule', 'calendar', 'today', 'appointments']
        },
        'recipe': {
            'hi': ['रेसिपी', 'खाना', 'बनाना', 'व्यंजन'],
            'en': ['recipe', 'cook', 'make', 'food', 'dish']
        },
        'calendar_add': {
            'hi': ['कैलेंडर में', 'जोड़', 'मीटिंग', 'शेड्यूल करें'],
            'en': ['add to calendar', 'schedule', 'add meeting', 'calendar']
        },
    }
    
    for intent, keywords in intents.items():
        for keyword in keywords.get(lang, keywords['en']):
            if keyword in text_lower:
                return intent
    
    return 'unknown'

class VoiceProcessor:
    """Process voice notes and extract reminders"""
    
    @staticmethod
    def download_media(media_url, account_sid, auth_token):
        """Download voice note from Twilio"""
        try:
            response = requests.get(
                media_url,
                auth=(account_sid, auth_token)
            )
            if response.status_code == 200:
                return response.content
            return None
        except Exception as e:
            logger.error(f"Error downloading media: {e}")
            return None
    
    @staticmethod
    def transcribe_audio(audio_data):
        """Transcribe audio to text using Google Speech Recognition"""
        try:
            recognizer = sr.Recognizer()
            
            # Save audio to temporary file
            with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp_file:
                tmp_file.write(audio_data)
                tmp_file_path = tmp_file.name
            
            # Convert to WAV and transcribe
            with sr.AudioFile(tmp_file_path) as source:
                audio = recognizer.record(source)
            
            # Try Hindi first, then English
            try:
                text_hi = recognizer.recognize_google(audio, language='hi-IN')
                text_en = recognizer.recognize_google(audio, language='en-IN')
                
                # Return the one with higher confidence
                # In practice, you'd check confidence scores
                return {
                    'success': True,
                    'text_hi': text_hi,
                    'text_en': text_en,
                    'primary_text': text_hi  # Default to Hindi
                }
            except:
                # Try English only
                text = recognizer.recognize_google(audio, language='en-IN')
                return {
                    'success': True,
                    'text_hi': None,
                    'text_en': text,
                    'primary_text': text
                }
            
        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            # Clean up temp file
            if 'tmp_file_path' in locals():
                os.unlink(tmp_file_path)
    
    @staticmethod
    def extract_tasks_from_text(text, lang='en'):
        """Extract multiple tasks and times from transcribed text"""
        tasks = []
        
        # Common patterns for task extraction
        task_keywords = {
            'hi': ['फिर', 'और', 'उसके बाद', 'भी', 'रिमाइंड', 'याद'],
            'en': ['then', 'and', 'also', 'after that', 'remind', 'remember']
        }
        
        # Split text into potential tasks
        sentences = re.split(r'[।\.,;]', text)
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # Check if sentence contains time/task indicators
            time_found = False
            if lang == 'hi':
                time_found = bool(re.search(r'\d+\s*बजे|\d+:\d+|सुबह|शाम|दोपहर|रात', sentence))
            else:
                time_found = bool(re.search(r'\d+\s*(am|pm|AM|PM)|\d+:\d+|morning|evening|afternoon|night', sentence))
            
            if time_found or any(keyword in sentence.lower() for keyword in task_keywords.get(lang, [])):
                # Try to parse this as a reminder
                reminder_data = ReminderService.parse_reminder(sentence, lang)
                if reminder_data['success']:
                    tasks.append(reminder_data)
        
        return tasks

class ProactiveMessaging:
    """Send proactive check-in messages"""
    
    @staticmethod
    def get_users_for_checkin():
        """Get users who should receive check-in messages"""
        users = []
        pattern = "user:*"
        
        for key in redis_client.scan_iter(pattern):
            user_data = redis_client.hgetall(key)
            phone_number = key.replace('user:', '')
            
            # Check last interaction time
            last_interaction = user_data.get('last_interaction')
            if last_interaction:
                last_time = datetime.fromisoformat(last_interaction)
                hours_since = (datetime.now() - last_time).total_seconds() / 3600
                
                # Only message if user has been active in last 7 days
                if hours_since < 168:  # 7 days
                    users.append({
                        'phone_number': phone_number,
                        'language': user_data.get('language', 'en'),
                        'assistant_name': user_data.get('assistant_name', 'Assistant'),
                        'timezone': user_data.get('timezone', 'Asia/Kolkata')
                    })
        
        return users
    
    @staticmethod
    def send_proactive_checkin(time_of_day='morning'):
        """Send proactive check-in messages"""
        users = ProactiveMessaging.get_users_for_checkin()
        
        for user in users:
            try:
                # Get user's local time
                tz = pytz.timezone(user['timezone'])
                local_time = datetime.now(tz)
                hour = local_time.hour
                
                # Determine appropriate message based on time
                if time_of_day == 'morning' and 7 <= hour <= 10:
                    message_key = 'proactive_morning'
                elif time_of_day == 'afternoon' and 14 <= hour <= 16:
                    message_key = 'proactive_afternoon'
                elif time_of_day == 'evening' and 18 <= hour <= 20:
                    message_key = 'proactive_evening'
                else:
                    continue  # Skip if not in appropriate time window
                
                # Send message
                message = MESSAGES[message_key][user['language']].format(
                    assistant_name=user['assistant_name']
                )
                
                twilio_client.messages.create(
                    body=message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=user['phone_number']
                )
                
                logger.info(f"Sent {time_of_day} check-in to {user['phone_number']}")
                
            except Exception as e:
                logger.error(f"Error sending proactive message: {e}")

# Schedule proactive messages
scheduler.add_job(
    func=lambda: ProactiveMessaging.send_proactive_checkin('morning'),
    trigger='cron',
    hour=8,
    minute=30,
    timezone='Asia/Kolkata',
    id='proactive_morning'
)

scheduler.add_job(
    func=lambda: ProactiveMessaging.send_proactive_checkin('afternoon'),
    trigger='cron',
    hour=14,
    minute=30,
    timezone='Asia/Kolkata',
    id='proactive_afternoon'
)

scheduler.add_job(
    func=lambda: ProactiveMessaging.send_proactive_checkin('evening'),
    trigger='cron',
    hour=18,
    minute=30,
    timezone='Asia/Kolkata',
    id='proactive_evening'
)
def extract_name_from_message(text, lang='en'):
    """Extract assistant name from message"""
    import re
    
    # Patterns to extract name
    patterns = {
        'en': [
            r'your name is (\w+)',
            r'call you (\w+)',
            r'name you (\w+)',
            r'i\'ll call you (\w+)'
        ],
        'hi': [
            r'तुम्हारा नाम (\w+)',
            r'आपका नाम (\w+)',
            r'नाम है (\w+)',
            r'(\w+) नाम है'
        ]
    }
    
    for pattern in patterns.get(lang, patterns['en']):
        match = re.search(pattern, text.lower())
        if match:
            return match.group(1).capitalize()
    
    return None
    """Format calendar events for display"""
    if not events:
        no_events = {
            'hi': 'आज कोई मीटिंग नहीं है। दिन अच्छा बिताएं! 🌸',
            'en': 'No meetings today. Have a great day! 🌸'
        }
        return no_events[lang]
    
    schedule_lines = []
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        if 'T' in start:
            time = datetime.fromisoformat(start.replace('Z', '+00:00'))
            time_str = time.strftime('%I:%M %p')
            title = event.get('summary', 'No title')
            schedule_lines.append(f"• {time_str} - {title}")
    
    return '\n'.join(schedule_lines)

def get_suggestions(time_of_day, lang='en'):
    """Get activity suggestions based on time"""
    hour = datetime.now().hour
    
    suggestions = {
        'morning': {
            'hi': ['🧘 योग या ध्यान करें', '📖 किताब पढ़ें', '🚶 मॉर्निंग वॉक पर जाएं'],
            'en': ['🧘 Do yoga or meditation', '📖 Read a book', '🚶 Go for a morning walk']
        },
        'afternoon': {
            'hi': ['☕ चाय का आनंद लें', '📝 दिन की योजना बनाएं', '🎵 संगीत सुनें'],
            'en': ['☕ Enjoy some tea', '📝 Plan your day', '🎵 Listen to music']
        },
        'evening': {
            'hi': ['🌅 सूर्यास्त देखें', '👨‍👩‍👧 परिवार के साथ समय बिताएं', '🍳 कुछ नया बनाएं'],
            'en': ['🌅 Watch the sunset', '👨‍👩‍👧 Spend time with family', '🍳 Try a new recipe']
        }
    }
    
    if 5 <= hour < 12:
        period = 'morning'
    elif 12 <= hour < 17:
        period = 'afternoon'
    else:
        period = 'evening'
    
    return '\n'.join(suggestions[period][lang])

@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        media_url = request.values.get('MediaUrl0', '')  # Voice note URL
        
        logger.info(f"Received from {from_number}: {incoming_msg if incoming_msg else 'Voice note'}")
        
        # Get user session
        session = UserSession(from_number)
        user_lang = session.get_language()
        assistant_name = session.get_assistant_name()
        
        # Update last interaction time
        user_data = session.get_data()
        user_data['last_interaction'] = datetime.now().isoformat()
        session.save_data(user_data)
        
        # Handle voice notes
        if media_url and not incoming_msg:
            # Send acknowledgment
            twilio_client.messages.create(
                body=MESSAGES['voice_received'][user_lang],
                from_=TWILIO_WHATSAPP_NUMBER,
                to=from_number
            )
            
            # Download and process voice note
            audio_data = VoiceProcessor.download_media(
                media_url,
                TWILIO_ACCOUNT_SID,
                TWILIO_AUTH_TOKEN
            )
            
            if audio_data:
                # Transcribe audio
                transcription = VoiceProcessor.transcribe_audio(audio_data)
                
                if transcription['success']:
                    # Extract tasks from transcription
                    text = transcription['primary_text']
                    detected_lang = 'hi' if transcription['text_hi'] else 'en'
                    tasks = VoiceProcessor.extract_tasks_from_text(text, detected_lang)
                    
                    if tasks:
                        # Set reminders for all extracted tasks
                        reminder_list = []
                        for task_data in tasks:
                            ReminderService.schedule_reminder(
                                from_number,
                                task_data['task'],
                                task_data['time'],
                                detected_lang
                            )
                            reminder_list.append(
                                f"• {task_data['task']} - {task_data['time'].strftime('%I:%M %p')}"
                            )
                        
                        response = MESSAGES['voice_processed'][user_lang].format(
                            reminders='\n'.join(reminder_list)
                        )
                    else:
                        # Couldn't extract specific tasks, show transcription
                        response = f"I heard: '{text}'\n\nCould you please specify the time for your reminders?"
                else:
                    response = "Sorry, I couldn't understand the voice note. Please try again or type your message."
            else:
                response = "Error processing voice note. Please try again."
            
            # Send response
            twilio_client.messages.create(
                body=response,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=from_number
            )
            return jsonify({'status': 'success'}), 200
        
        # Get user session
        session = UserSession(from_number)
        user_lang = session.get_language()
        assistant_name = session.get_assistant_name()
        
        # Detect language from message
        try:
            detected_lang = translator.detect(incoming_msg).lang
            if detected_lang in ['hi', 'en']:
                user_lang = detected_lang
                session.set_language(user_lang)
        except:
            pass
        
        # Detect intent
        intent = detect_intent(incoming_msg, user_lang)
        
        # Process based on intent
        if intent == 'greeting':
            # Check if user has set a name for the assistant
            if assistant_name != 'Assistant':
                response = MESSAGES['introduction'][user_lang].format(name=assistant_name)
            else:
                response = MESSAGES['welcome'][user_lang]
        
        elif intent == 'set_name':
            # Extract name from message
            new_name = extract_name_from_message(incoming_msg, user_lang)
            if new_name:
                session.set_assistant_name(new_name)
                response = MESSAGES['name_set'][user_lang].format(name=new_name)
            else:
                if user_lang == 'hi':
                    response = "कृपया बताएं आप मुझे क्या नाम देना चाहते हैं? उदाहरण: 'तुम्हारा नाम राज है'"
                else:
                    response = "Please tell me what name you'd like to give me? Example: 'Your name is Raj'"
        
        elif intent == 'reminder':
            reminder_data = ReminderService.parse_reminder(incoming_msg, user_lang)
            if reminder_data['success']:
                ReminderService.schedule_reminder(
                    from_number,
                    reminder_data['task'],
                    reminder_data['time'],
                    user_lang
                )
                response = MESSAGES['reminder_set'][user_lang].format(
                    task=reminder_data['task'],
                    date=reminder_data['time'].strftime('%d/%m/%Y'),
                    time=reminder_data['time'].strftime('%I:%M %p')
                )
            else:
                response = "Please specify time. Example: 'Remind me tomorrow at 9 AM'"
        
        elif intent == 'schedule':
            events = CalendarService.get_today_events()
            schedule = format_schedule(events, user_lang)
            quote = QUOTES[user_lang][datetime.now().day % len(QUOTES[user_lang])]
            response = MESSAGES['morning_greeting'][user_lang].format(
                date=datetime.now().strftime('%d %B %Y'),
                schedule=schedule,
                quote=quote
            )
        
        elif intent == 'recipe':
            # Extract dish name
            dish_keywords = ['paneer', 'पनीर', 'dal', 'दाल']
            dish_found = None
            for dish in dish_keywords:
                if dish in incoming_msg.lower():
                    dish_found = 'paneer' if 'paneer' in dish or 'पनीर' in dish else 'dal'
                    break
            
            if dish_found and dish_found in RECIPES:
                recipe = RECIPES[dish_found][user_lang]
                response = MESSAGES['recipe_found'][user_lang].format(
                    dish=recipe['name'],
                    ingredients=recipe['ingredients'],
                    method=recipe['method'],
                    time=recipe['time']
                )
            else:
                response = "Available recipes: Paneer Butter Masala, Dal Tadka"
        
        elif intent == 'calendar_add':
            # Parse calendar command
            cal_data = CalendarService.parse_calendar_command(incoming_msg, user_lang)
            if cal_data['success']:
                # Create calendar event
                result = CalendarService.create_event(
                    title=cal_data['title'],
                    date_time=cal_data['datetime'],
                    duration_minutes=cal_data['duration']
                )
                
                if result['success']:
                    response = MESSAGES['calendar_add'][user_lang].format(
                        title=cal_data['title'],
                        date=cal_data['datetime'].strftime('%d/%m/%Y'),
                        time=cal_data['datetime'].strftime('%I:%M %p'),
                        duration=cal_data['duration'],
                        link=result['link']
                    )
                else:
                    response = MESSAGES['calendar_error'][user_lang]
            else:
                response = MESSAGES['calendar_error'][user_lang]
        
        elif intent == 'suggestion':
            suggestions = get_suggestions(datetime.now().hour, user_lang)
            response = MESSAGES['suggestion'][user_lang].format(suggestions=suggestions)
        
        else:
            response = MESSAGES['welcome'][user_lang]
        
        # Send response
        message = twilio_client.messages.create(
            body=response,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=from_number
        )
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/handle-reminder-response', methods=['POST'])
def handle_reminder_response():
    """Handle response from reminder calls"""
    digit_pressed = request.values.get('Digits', '')
    
    response = VoiceResponse()
    
    if digit_pressed == '1':
        response.say("Thank you. Reminder confirmed.", voice='Polly.Raveena')
    elif digit_pressed == '2':
        response.say("Reminder snoozed for 10 minutes.", voice='Polly.Raveena')
        # Logic to reschedule reminder
    else:
        response.say("Invalid input. Goodbye.", voice='Polly.Raveena')
    
    return str(response)

@app.route('/morning-scheduler', methods=['GET'])
def trigger_morning_messages():
    """Manually trigger morning messages (for testing)"""
    send_morning_messages()
    return jsonify({'status': 'Morning messages sent'}), 200

def send_morning_messages():
    """Send morning greetings to all users"""
    # Get all users from Redis
    users = []
    for key in redis_client.scan_iter("user:*"):
        user_data = redis_client.hgetall(key)
        phone_number = key.replace('user:', '')
        users.append({
            'phone_number': phone_number,
            'language': user_data.get('language', 'en'),
            'name': user_data.get('name', 'Friend')
        })
    
    for user in users:
        try:
            # Get calendar events
            events = CalendarService.get_today_events()
            schedule = format_schedule(events, user['language'])
            
            # Get quote
            quote = QUOTES[user['language']][datetime.now().day % len(QUOTES[user['language']])]
            
            # Get assistant name
            assistant_name = UserSession(user['phone_number']).get_assistant_name()
            
            # Format message with assistant's signature
            message = MESSAGES['morning_greeting'][user['language']].format(
                date=datetime.now().strftime('%d %B %Y'),
                schedule=schedule,
                quote=quote
            )
            
            # Add assistant's signature
            if user['language'] == 'hi':
                message += f"\n\n- आपका {assistant_name} 🤖"
            else:
                message += f"\n\n- Your {assistant_name} 🤖"
            
            # Send message
            twilio_client.messages.create(
                body=message,
                from_=TWILIO_WHATSAPP_NUMBER,
                to=user['phone_number']
            )
            
            logger.info(f"Morning message sent to {user['phone_number']}")
            
        except Exception as e:
            logger.error(f"Error sending morning message to {user['phone_number']}: {e}")

# Schedule morning messages
scheduler.add_job(
    func=send_morning_messages,
    trigger='cron',
    hour=7,
    minute=0,
    timezone='Asia/Kolkata',
    id='morning_messages'
)

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'services': {
            'redis': redis_client.ping(),
            'scheduler': scheduler.running
        }
    }), 200

if __name__ == '__main__':
    # In production, use gunicorn
    app.run(host='0.0.0.0', port=8080, debug=False)