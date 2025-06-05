import os
import json
import pytz
import logging
import requests
import random
import phonenumbers
from phonenumbers import timezone as phone_timezone
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from dotenv import load_dotenv
import re
from typing import Dict, List, Optional, Tuple
import sqlite3
import threading

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize scheduler
jobstores = {
    'default': MemoryJobStore()
}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone='UTC')
scheduler.start()

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_API_KEY = os.getenv('TWILIO_API_KEY')
TWILIO_API_SECRET = os.getenv('TWILIO_API_SECRET')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')

# OpenAI is optional - bot works without it
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
USE_AI = bool(OPENAI_API_KEY)

# Initialize Twilio client
twilio_client = Client(TWILIO_API_KEY, TWILIO_API_SECRET, account_sid=TWILIO_ACCOUNT_SID)

# Initialize OpenAI if available
if USE_AI:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        logger.info("AI mode enabled with OpenAI")
    except:
        USE_AI = False
        logger.info("OpenAI not available, using smart pattern matching")

# Initialize database
def init_db():
    conn = sqlite3.connect('assistant.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (phone_number TEXT PRIMARY KEY,
                  name TEXT,
                  assistant_name TEXT DEFAULT 'Assistant',
                  timezone TEXT,
                  preferences TEXT,
                  created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  phone_number TEXT,
                  role TEXT,
                  content TEXT,
                  timestamp TIMESTAMP,
                  FOREIGN KEY (phone_number) REFERENCES users(phone_number))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS goals
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  phone_number TEXT,
                  goal TEXT,
                  check_ins_scheduled INTEGER DEFAULT 0,
                  completed BOOLEAN DEFAULT 0,
                  created_at TIMESTAMP,
                  FOREIGN KEY (phone_number) REFERENCES users(phone_number))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS reminders
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  phone_number TEXT,
                  task TEXT,
                  reminder_time TIMESTAMP,
                  completed BOOLEAN DEFAULT 0,
                  created_at TIMESTAMP,
                  FOREIGN KEY (phone_number) REFERENCES users(phone_number))''')
    
    conn.commit()
    conn.close()

init_db()

class PersonalAssistant:
    """Smart personal assistant that works with or without AI"""
    
    def __init__(self, phone_number: str):
        self.phone_number = phone_number
        self.user = self.get_or_create_user()
        self.timezone = pytz.timezone(self.user['timezone'])
        
    def get_or_create_user(self) -> Dict:
        """Get or create user with timezone detection"""
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        c.execute("SELECT * FROM users WHERE phone_number = ?", (self.phone_number,))
        user = c.fetchone()
        
        if not user:
            # Detect timezone from phone
            timezone = self.detect_timezone()
            
            c.execute("""INSERT INTO users (phone_number, name, timezone, created_at)
                        VALUES (?, ?, ?, ?)""",
                     (self.phone_number, 'Friend', timezone, datetime.now()))
            conn.commit()
            
            user = {
                'phone_number': self.phone_number,
                'name': 'Friend',
                'assistant_name': 'Assistant',
                'timezone': timezone,
                'preferences': {}
            }
            
            logger.info(f"New user created: {self.phone_number} in timezone {timezone}")
        else:
            user = {
                'phone_number': user[0],
                'name': user[1],
                'assistant_name': user[2] or 'Assistant',
                'timezone': user[3],
                'preferences': json.loads(user[4]) if user[4] else {}
            }
        
        conn.close()
        return user
    
    def detect_timezone(self) -> str:
        """Detect timezone from phone number"""
        try:
            clean_number = self.phone_number.replace('whatsapp:', '')
            parsed = phonenumbers.parse(clean_number, None)
            timezones = phone_timezone.time_zones_for_number(parsed)
            
            if timezones:
                return timezones[0]
            
            # Default by country code
            country_code = parsed.country_code
            defaults = {
                91: 'Asia/Kolkata',
                1: 'America/New_York',
                44: 'Europe/London',
                86: 'Asia/Shanghai',
                81: 'Asia/Tokyo'
            }
            return defaults.get(country_code, 'UTC')
            
        except:
            return 'UTC'
    
    def process_message(self, message: str) -> str:
        """Process message with AI or smart patterns"""
        # Save conversation
        self.save_conversation("user", message)
        
        # Try AI first if available
        if USE_AI and self.should_use_ai(message):
            try:
                response = self.process_with_ai(message)
                self.save_conversation("assistant", response)
                return response
            except Exception as e:
                logger.error(f"AI processing failed: {e}")
                # Fall back to pattern matching
        
        # Use smart pattern matching
        response = self.process_with_patterns(message)
        self.save_conversation("assistant", response)
        return response
    
    def should_use_ai(self, message: str) -> bool:
        """Determine if AI is needed for this request"""
        # Use AI for complex requests
        ai_triggers = [
            'help me', 'what should i', 'how do i', 'advice', 'plan',
            'analyze', 'suggest', 'recommend', 'think about', 'strategy',
            'ideas', 'brainstorm', 'explain', 'why', 'understand'
        ]
        return any(trigger in message.lower() for trigger in ai_triggers)
    
    def process_with_ai(self, message: str) -> str:
        """Process with OpenAI"""
        # Get conversation history
        history = self.get_conversation_history()
        
        messages = [
            {
                "role": "system",
                "content": f"""You are {self.user['assistant_name']}, a helpful personal assistant.
                User's timezone: {self.user['timezone']}
                Current time: {datetime.now(self.timezone).strftime('%I:%M %p')}
                Be conversational, helpful, and proactive. If they ask to set reminders or goals, 
                acknowledge it and I'll handle the scheduling."""
            }
        ]
        
        # Add recent history
        for h in history[-10:]:
            messages.append({"role": h["role"], "content": h["content"]})
        
        messages.append({"role": "user", "content": message})
        
        # Use GPT-3.5 for cost efficiency
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.7,
            max_tokens=300
        )
        
        ai_response = response.choices[0].message['content']
        
        # Extract any actions from the response
        self.extract_and_execute_actions(message, ai_response)
        
        return ai_response
    
    def process_with_patterns(self, message: str) -> str:
        """Smart pattern matching without AI"""
        msg_lower = message.lower().strip()
        current_time = datetime.now(self.timezone)
        
        # Greeting or help
        if any(word in msg_lower for word in ['hi', 'hello', 'hey', 'start', 'help']):
            return f"""👋 Hi! I'm {self.user['assistant_name']}, your personal assistant!

I can help you:
📅 Set reminders - "Remind me to call mom at 5 PM"
🎯 Track goals - "I want to write 1500 words today"
🍳 Find recipes - "Show me a pasta recipe"  
⏰ Manage your time - "What should I do now?"
📝 Stay organized - "My tasks for today are..."
✏️ Name me - "I'll call you Jarvis"

Your timezone: {self.user['timezone']} ({current_time.strftime('%I:%M %p')})

What would you like help with?"""

        # Set assistant name
        elif any(phrase in msg_lower for phrase in ["i'll call you", "your name is", "call you", "name you"]):
            # Extract name more intelligently
            patterns = [
                r"i'll call you\s+(\w+)",
                r"your name is\s+(\w+)",
                r"call you\s+(\w+)",
                r"name you\s+(\w+)"
            ]
            
            for pattern in patterns:
                match = re.search(pattern, msg_lower)
                if match:
                    new_name = match.group(1).capitalize()
                    self.update_assistant_name(new_name)
                    return f"Great! I'm {new_name} now. How can I help you today? 😊"
            
            return "What would you like to call me? Just say 'I'll call you [name]'"

        # Show reminders/status - check this BEFORE reminder handling
        elif any(phrase in msg_lower for phrase in ['show my reminders', 'my reminders', 'show reminders', 'show status', 'my tasks', 'what do i have', 'whats pending', 'pending']):
            return self.get_status()

        # Reminders - check AFTER status commands
        elif 'remind' in msg_lower:
            return self.handle_reminder(message)

        # Quick confirmations for ongoing tasks
        elif msg_lower in ['yes', 'yeah', 'sure', 'ok', 'set it as a goal', 'make it a goal']:
            # Check if there was a recent reminder with an ongoing task
            conn = sqlite3.connect('assistant.db')
            c = conn.cursor()
            c.execute("""SELECT task FROM reminders 
                        WHERE phone_number = ? 
                        ORDER BY created_at DESC LIMIT 1""",
                     (self.phone_number,))
            recent = c.fetchone()
            conn.close()
            
            if recent:
                task = recent[0]
                ongoing_keywords = ['essay', 'report', 'project', 'assignment', 'homework', 'study', 'work on', 'write', 'finish', 'complete']
                if any(keyword in task.lower() for keyword in ongoing_keywords):
                    # Convert to a goal
                    goal_text = task.replace('you need to', '').replace('your', 'my').strip()
                    goal_text = goal_text[0].upper() + goal_text[1:] if goal_text else task
                    return self.handle_goal(f"I want to {goal_text}")
            
            return "What would you like to set as a goal? Just tell me what you want to accomplish!"

        # Goals
        elif any(phrase in msg_lower for phrase in ['i want to', 'i need to', 'my goal', 'i have to', 'i must']):
            return self.handle_goal(message)

        # Recipe search
        elif any(word in msg_lower for word in ['recipe', 'cook', 'food', 'meal', 'dish']):
            return self.search_recipe(message)

        # Progress update
        elif any(word in msg_lower for word in ['done', 'completed', 'finished', 'did it']):
            return self.handle_completion(message)

        # Time management
        elif 'what should i do' in msg_lower or 'bored' in msg_lower:
            return self.suggest_activity()

        # Morning routine
        elif any(phrase in msg_lower for phrase in ['good morning', 'morning routine', 'start my day']):
            return self.morning_routine()

        # Default helpful response
        else:
            return self.smart_fallback(message)
    
    def handle_reminder(self, message: str) -> str:
        """Handle reminder requests"""
        # Parse time and task
        reminder_time, task = self.parse_reminder(message)
        
        if reminder_time and task:
            # Schedule the reminder
            job_id = f"reminder_{self.phone_number}_{reminder_time.timestamp()}"
            
            scheduler.add_job(
                func=send_reminder,
                trigger='date',
                run_date=reminder_time,
                args=[self.phone_number, task, self.user['assistant_name']],
                id=job_id,
                replace_existing=True
            )
            
            # Save to database
            conn = sqlite3.connect('assistant.db')
            c = conn.cursor()
            c.execute("""INSERT INTO reminders (phone_number, task, reminder_time, created_at)
                        VALUES (?, ?, ?, ?)""",
                     (self.phone_number, task, reminder_time, datetime.now()))
            conn.commit()
            conn.close()
            
            time_str = reminder_time.strftime('%I:%M %p').lstrip('0')
            date_str = "today" if reminder_time.date() == datetime.now(self.timezone).date() else reminder_time.strftime('%B %d')
            
            response = f"""✅ Reminder set!

📌 Task: {task}
⏰ Time: {time_str} {date_str}

I'll message you then!"""
            
            # Check if this is an ongoing task that needs follow-ups
            ongoing_keywords = ['essay', 'report', 'project', 'assignment', 'homework', 'study', 'work on', 'write', 'finish', 'complete', 'class', 'exam', 'test']
            if any(keyword in task.lower() for keyword in ongoing_keywords):
                response += f"""

I noticed this might be an ongoing task. Would you like me to:
• Set up regular check-ins to help you stay on track?
• Create a goal to track your progress?

Just say "yes" or "set it as a goal" and I'll help you stay accountable! 💪"""
            else:
                response += "\n\n💡 Tip: Say \"show my reminders\" to see all pending reminders."
            
            return response
        else:
            # Try to be more helpful about what went wrong
            msg_lower = message.lower()
            
            # Check if they included a time at all
            has_time_words = any(word in msg_lower for word in ['at', 'in', 'by', 'tomorrow', 'tonight', 'morning', 'afternoon', 'evening'])
            
            if not has_time_words:
                return """I didn't catch when you want to be reminded. Try including a time:

✓ "Remind me in 30 minutes to call mom"
✓ "Remind me at 5:30 PM to go to my economics class"
✓ "Remind me tomorrow morning to submit the report"
✓ "Remind me tonight at 8 to watch the game"

When should I remind you?"""
            else:
                # They tried to include a time but we couldn't parse it
                return """I'm having trouble understanding the time. Here are formats that work:

✓ "Remind me to call John at 5:30 PM"
✓ "Remind me to study in 2 hours"
✓ "Remind me at noon to eat lunch"
✓ "Remind me tomorrow at 9 AM to submit report"

I can understand:
• Specific times: "at 3:45 PM", "at 5PM", "by 6:30 PM"
• Relative times: "in 20 minutes", "in 1 hour"
• Natural times: "tomorrow morning", "tonight at 8"

What would you like me to remind you about?"""
    
    def parse_reminder(self, text: str) -> Tuple[Optional[datetime], Optional[str]]:
        """Parse reminder time and task from text - handles natural language"""
        text_lower = text.lower()
        now = datetime.now(self.timezone)
        
        # Remove "remind me" and variations to clean up the text
        cleaned_text = text
        for phrase in ['remind me', 'reminder', 'remind']:
            cleaned_text = re.sub(rf'\b{phrase}\b', '', cleaned_text, flags=re.IGNORECASE)
        cleaned_text = cleaned_text.strip()
        
        # If it starts with "to", remove it
        if cleaned_text.startswith('to '):
            cleaned_text = cleaned_text[3:]
        
        reminder_time = None
        task = None
        
        # Pattern 1: "in X minutes/hours"
        relative_patterns = [
            r'in\s+(\d+)\s*(minute|min|hour|hr)s?',
            r'after\s+(\d+)\s*(minute|min|hour|hr)s?',
            r'(\d+)\s*(minute|min|hour|hr)s?\s+from\s+now',
        ]
        
        for pattern in relative_patterns:
            match = re.search(pattern, text_lower)
            if match:
                amount = int(match.group(1))
                unit = match.group(2).lower()
                
                if 'hour' in unit or 'hr' in unit:
                    reminder_time = now + timedelta(hours=amount)
                else:
                    reminder_time = now + timedelta(minutes=amount)
                
                # Extract task by removing the time part
                task = cleaned_text
                task = task.replace(match.group(0), '')
                break
        
        # Pattern 2: Specific times with various formats
        if not reminder_time:
            time_patterns = [
                # "at 5:30 PM", "at 5:30PM", "at 530 PM"
                r'at\s+(\d{1,2}):?(\d{0,2})\s*(am|pm|AM|PM)',
                # "by 5:30 PM"
                r'by\s+(\d{1,2}):?(\d{0,2})\s*(am|pm|AM|PM)',
                # "5:30 PM" anywhere in the text
                r'(\d{1,2}):(\d{2})\s*(am|pm|AM|PM)',
                # "5 PM" anywhere in the text
                r'(\d{1,2})\s*(am|pm|AM|PM)',
                # "tomorrow at 5 PM"
                r'tomorrow\s+at\s+(\d{1,2}):?(\d{0,2})\s*(am|pm|AM|PM)',
                # "tonight at 8"
                r'tonight\s+at\s+(\d{1,2}):?(\d{0,2})',
            ]
            
            for pattern in time_patterns:
                match = re.search(pattern, text_lower)
                if match:
                    groups = match.groups()
                    hour = int(groups[0])
                    
                    # Handle minutes if present
                    if len(groups) > 2 and groups[1] and groups[1].isdigit():
                        minute = int(groups[1])
                    else:
                        minute = 0
                    
                    # Handle AM/PM if present
                    if len(groups) >= 3 and groups[-1] and groups[-1].lower() in ['am', 'pm']:
                        period = groups[-1].lower()
                        if period == 'pm' and hour != 12:
                            hour += 12
                        elif period == 'am' and hour == 12:
                            hour = 0
                    elif 'tonight' in text_lower and hour < 12:
                        hour += 12  # Assume PM for "tonight"
                    
                    try:
                        reminder_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        
                        # Handle relative days
                        if 'tomorrow' in text_lower:
                            reminder_time += timedelta(days=1)
                        elif 'tonight' in text_lower and reminder_time <= now:
                            # If "tonight" but time has passed, assume tomorrow
                            reminder_time += timedelta(days=1)
                        elif reminder_time <= now:
                            # If time has passed today, assume tomorrow
                            reminder_time += timedelta(days=1)
                        
                        # Extract task
                        task = cleaned_text
                        # Remove the time pattern and surrounding words
                        time_phrase = match.group(0)
                        # Also remove "at", "by", "around" before the time
                        task = re.sub(rf'(at|by|around)?\s*{re.escape(time_phrase)}', '', task, flags=re.IGNORECASE)
                        task = re.sub(r'\btomorrow\b', '', task, flags=re.IGNORECASE)
                        task = re.sub(r'\btonight\b', '', task, flags=re.IGNORECASE)
                        break
                    except ValueError:
                        continue
        
        # Pattern 3: Relative times like "in the morning", "this afternoon"
        if not reminder_time:
            relative_times = {
                'morning': (9, 0),
                'afternoon': (14, 0),
                'evening': (18, 0),
                'night': (21, 0),
                'noon': (12, 0),
                'midnight': (0, 0)
            }
            
            for time_name, (hour, minute) in relative_times.items():
                if time_name in text_lower:
                    reminder_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if reminder_time <= now:
                        reminder_time += timedelta(days=1)
                    
                    # Extract task
                    task = cleaned_text
                    task = re.sub(rf'(in\s+the\s+)?{time_name}', '', task, flags=re.IGNORECASE)
                    task = re.sub(r'\b(this|tomorrow)\b', '', task, flags=re.IGNORECASE)
                    break
        
        # Pattern 4: Handle "to [task] at/in [time]" format
        if reminder_time and task:
            # Clean up the task
            task = ' '.join(task.split()).strip()
            
            # Remove common filler words at the start
            for word in ['to', 'that', 'about', 'for']:
                if task.startswith(f'{word} '):
                    task = task[len(word)+1:]
            
            # Fix pronouns
            task = re.sub(r'\bi\b', 'you', task, flags=re.IGNORECASE)
            task = re.sub(r'\bmy\b', 'your', task, flags=re.IGNORECASE)
            task = re.sub(r'\bme\b', 'you', task, flags=re.IGNORECASE)
            task = re.sub(r"\bi'm\b", "you're", task, flags=re.IGNORECASE)
            task = re.sub(r"\bi've\b", "you've", task, flags=re.IGNORECASE)
            
            # Ensure task starts with lowercase unless it's a proper noun
            if task and not any(word[0].isupper() for word in task.split() if word not in ['i', "i'm", "i've"]):
                task = task[0].lower() + task[1:] if len(task) > 1 else task.lower()
            
            # If task is empty or too short, use the full cleaned text
            if not task or len(task) < 3:
                task = "your reminder"
        
        return reminder_time, task
    
    def get_status(self) -> str:
        """Get current status of tasks and goals"""
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        # Get pending reminders
        c.execute("""SELECT task, reminder_time FROM reminders 
                    WHERE phone_number = ? AND completed = 0 
                    AND reminder_time > datetime('now')
                    ORDER BY reminder_time LIMIT 5""",
                 (self.phone_number,))
        reminders = c.fetchall()
        
        # Get active goals
        c.execute("""SELECT goal, created_at FROM goals 
                    WHERE phone_number = ? AND completed = 0
                    ORDER BY created_at DESC LIMIT 5""",
                 (self.phone_number,))
        goals = c.fetchall()
        
        conn.close()
        
        # More conversational response
        if not reminders and not goals:
            return """You're all clear! 🎉 

No reminders or goals pending right now. 

Want to:
• Set a reminder? Say "Remind me to..."
• Set a goal? Say "I want to..."
• Get suggestions? Say "What should I do?"

I'm here whenever you need me!"""
        
        response = "Here's what you've got going on:\n\n"
        
        if reminders:
            response += "📌 **Your Reminders:**\n"
            for task, time_str in reminders:
                reminder_time = datetime.fromisoformat(time_str)
                # Convert to user's timezone for display
                reminder_time = reminder_time.replace(tzinfo=pytz.UTC).astimezone(self.timezone)
                time_until = reminder_time - datetime.now(self.timezone)
                
                # Natural time descriptions
                if time_until.days > 0:
                    time_desc = f"in {time_until.days} day{'s' if time_until.days > 1 else ''}"
                elif time_until.total_seconds() / 3600 > 1:
                    hours = int(time_until.total_seconds() / 3600)
                    time_desc = f"in {hours} hour{'s' if hours > 1 else ''}"
                else:
                    minutes = int(time_until.total_seconds() / 60)
                    if minutes < 1:
                        time_desc = "any moment now!"
                    elif minutes == 1:
                        time_desc = "in 1 minute"
                    else:
                        time_desc = f"in {minutes} minutes"
                
                response += f"• {task} ({time_desc})\n"
        
        if reminders and goals:
            response += "\n"
        
        if goals:
            response += "🎯 **Your Goals:**\n"
            for goal, created in goals:
                created_date = datetime.fromisoformat(created).date()
                days_active = (datetime.now().date() - created_date).days
                if days_active == 0:
                    day_text = "Started today"
                elif days_active == 1:
                    day_text = "Day 2"
                else:
                    day_text = f"Day {days_active + 1}"
                response += f"• {goal} ({day_text})\n"
        
        response += "\nLet me know if you complete anything! Just say 'done' 😊"
        
        return response
    
    def handle_goal(self, message: str) -> str:
        """Handle goal setting with smart check-ins"""
        # Extract the goal
        goal_text = message
        
        # Clean up common prefixes
        for prefix in ['i want to', 'i need to', 'my goal is to', 'i have to', 'i must']:
            if prefix in goal_text.lower():
                goal_text = goal_text.lower().replace(prefix, '', 1)
                break
        
        goal_text = goal_text.strip().capitalize()
        
        # Save goal
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        c.execute("""INSERT INTO goals (phone_number, goal, created_at)
                    VALUES (?, ?, ?)""",
                 (self.phone_number, goal_text, datetime.now()))
        goal_id = c.lastrowid
        conn.commit()
        conn.close()
        
        # Schedule smart check-ins based on goal type
        check_in_times = self.determine_checkin_schedule(goal_text)
        
        for i, hours in enumerate(check_in_times):
            check_time = datetime.now(self.timezone) + timedelta(hours=hours)
            job_id = f"goal_checkin_{self.phone_number}_{goal_id}_{i}"
            
            scheduler.add_job(
                func=send_goal_checkin,
                trigger='date',
                run_date=check_time,
                args=[self.phone_number, goal_text, self.user['assistant_name'], i+1],
                id=job_id,
                replace_existing=True
            )
        
        return f"""🎯 Goal set! I'll help you: {goal_text}

I'll check in with you:
• In {check_in_times[0]} hours - Quick progress check
• In {check_in_times[1]} hours - Mid-point review  
• In {check_in_times[2]} hours - Final push reminder

🔥 Let's make it happen! Reply anytime with updates or if you need help.

💡 Tip: Break it into smaller tasks if it feels overwhelming."""
    
    def determine_checkin_schedule(self, goal: str) -> List[int]:
        """Determine check-in schedule based on goal type"""
        goal_lower = goal.lower()
        
        if any(word in goal_lower for word in ['write', 'writing', 'essay', 'report', 'document']):
            return [2, 4, 6]  # Writing needs frequent check-ins
        elif any(word in goal_lower for word in ['study', 'learn', 'read', 'chapter', 'course']):
            return [1.5, 3, 5]  # Study goals need early check-in
        elif any(word in goal_lower for word in ['exercise', 'workout', 'gym', 'run', 'walk']):
            return [3, 6, 9]  # Exercise goals need less frequent checks
        elif any(word in goal_lower for word in ['call', 'email', 'contact', 'reach out']):
            return [1, 2, 4]  # Communication tasks need quick reminders
        else:
            return [2, 4, 7]  # Default schedule
    
    def search_recipe(self, query: str) -> str:
        """Search for recipes online"""
        try:
            # Extract the dish name
            dish = query.lower()
            for word in ['recipe', 'for', 'make', 'cook', 'find', 'show', 'me', 'a', 'how', 'to']:
                dish = dish.replace(word, '')
            dish = dish.strip()
            
            if not dish:
                return "What would you like to cook? Try: 'chicken recipe' or 'pasta recipe'"
            
            # Search using free API
            response = requests.get(f'https://www.themealdb.com/api/json/v1/1/search.php?s={dish}')
            
            if response.status_code == 200:
                data = response.json()
                meals = data.get('meals', [])
                
                if meals:
                    meal = meals[0]
                    
                    # Get ingredients
                    ingredients = []
                    for i in range(1, 21):
                        ingredient = meal.get(f'strIngredient{i}', '').strip()
                        measure = meal.get(f'strMeasure{i}', '').strip()
                        if ingredient:
                            ingredients.append(f"• {measure} {ingredient}".strip())
                    
                    recipe = f"""🍳 **{meal.get('strMeal', dish.title())}**

📝 **Ingredients:**
{chr(10).join(ingredients[:10])}

👨‍🍳 **Instructions:**
{meal.get('strInstructions', '')[:400]}...

🌍 Cuisine: {meal.get('strArea', 'International')}

Want the full recipe or a different dish? Just ask!"""
                    
                    return recipe
                else:
                    # Try broader search
                    return f"""I couldn't find a specific recipe for '{dish}'. 

Here are some popular options I can search for:
🍝 Pasta dishes - "pasta recipe"
🍗 Chicken meals - "chicken recipe"  
🥘 Rice dishes - "rice recipe"
🥗 Salads - "salad recipe"
🍛 Curry - "curry recipe"

What sounds good to you?"""
        
        except Exception as e:
            logger.error(f"Recipe search error: {e}")
            return "I'm having trouble searching recipes right now. Try asking for: chicken, pasta, salad, or rice recipes!"
    
    def suggest_activity(self) -> str:
        """Suggest activities based on time and context"""
        current_hour = datetime.now(self.timezone).hour
        
        # Get pending tasks
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        c.execute("""SELECT goal FROM goals 
                    WHERE phone_number = ? AND completed = 0
                    ORDER BY created_at DESC LIMIT 3""",
                 (self.phone_number,))
        pending_goals = [row[0] for row in c.fetchall()]
        
        c.execute("""SELECT task, reminder_time FROM reminders 
                    WHERE phone_number = ? AND completed = 0 
                    AND reminder_time > datetime('now')
                    ORDER BY reminder_time LIMIT 3""",
                 (self.phone_number,))
        upcoming_reminders = c.fetchall()
        conn.close()
        
        suggestions = []
        
        # Time-based suggestions
        if 5 <= current_hour < 9:
            suggestions.extend([
                "🌅 Start with 10 minutes of stretching",
                "☕ Make your favorite morning beverage mindfully",
                "📝 Write down 3 priorities for today",
                "🎵 Listen to energizing music while getting ready"
            ])
        elif 9 <= current_hour < 12:
            suggestions.extend([
                "🎯 Tackle your most important task while energy is high",
                "📧 Clear your inbox and messages",
                "🧠 Work on something requiring deep focus",
                "📞 Make important calls before lunch"
            ])
        elif 12 <= current_hour < 14:
            suggestions.extend([
                "🥗 Take a proper lunch break away from screens",
                "🚶 Go for a 15-minute walk",
                "💬 Connect with a friend or colleague",
                "🧘 Do a quick meditation"
            ])
        elif 14 <= current_hour < 17:
            suggestions.extend([
                "✅ Review and complete smaller tasks",
                "📊 Plan tomorrow's priorities",
                "🤝 Schedule meetings or calls",
                "📚 Learn something new for 20 minutes"
            ])
        elif 17 <= current_hour < 20:
            suggestions.extend([
                "🏃 Exercise or go for a walk",
                "🍳 Cook a healthy dinner",
                "📱 Call family or friends",
                "🎨 Work on a hobby or creative project"
            ])
        else:
            suggestions.extend([
                "📖 Read for 30 minutes",
                "🛁 Take a relaxing bath or shower",
                "📝 Journal about your day",
                "🌙 Start winding down for better sleep"
            ])
        
        response = f"Here are some suggestions for right now ({datetime.now(self.timezone).strftime('%I:%M %p')}):\n\n"
        response += "\n".join(random.sample(suggestions, min(4, len(suggestions))))
        
        if pending_goals:
            response += f"\n\n📋 **Your active goals:**\n"
            response += "\n".join([f"• {goal}" for goal in pending_goals])
            response += "\n\nWould you like to work on any of these?"
        
        if upcoming_reminders:
            response += f"\n\n⏰ **Upcoming reminders:**\n"
            for task, time_str in upcoming_reminders[:2]:
                reminder_time = datetime.fromisoformat(time_str)
                time_until = reminder_time - datetime.now()
                hours = time_until.total_seconds() / 3600
                if hours < 1:
                    time_desc = f"in {int(time_until.total_seconds() / 60)} minutes"
                else:
                    time_desc = f"in {int(hours)} hours"
                response += f"• {task} ({time_desc})\n"
        
        return response
    
    def morning_routine(self) -> str:
        """Provide morning routine with schedule"""
        current_time = datetime.now(self.timezone)
        
        # Get today's tasks
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        c.execute("""SELECT task, reminder_time FROM reminders 
                    WHERE phone_number = ? AND completed = 0 
                    AND date(reminder_time) = date('now')
                    ORDER BY reminder_time""",
                 (self.phone_number,))
        todays_reminders = c.fetchall()
        
        c.execute("""SELECT goal FROM goals 
                    WHERE phone_number = ? AND completed = 0""",
                 (self.phone_number,))
        active_goals = [row[0] for row in c.fetchall()]
        
        conn.close()
        
        response = f"""🌅 Good morning! It's {current_time.strftime('%I:%M %p, %A, %B %d')}

☀️ **Here's your day:**\n"""
        
        if todays_reminders:
            response += "\n📅 **Today's Schedule:**\n"
            for task, time_str in todays_reminders:
                time_obj = datetime.fromisoformat(time_str)
                response += f"• {time_obj.strftime('%I:%M %p')} - {task}\n"
        else:
            response += "\n📅 No scheduled reminders for today - a fresh canvas!\n"
        
        if active_goals:
            response += f"\n🎯 **Active Goals ({len(active_goals)}):**\n"
            for goal in active_goals[:3]:
                response += f"• {goal}\n"
        
        response += f"""
💪 **Morning Suggestions:**
• Start with 5 minutes of stretching
• Drink a glass of water
• Review your priorities
• Set an intention for the day

What would you like to focus on first?"""
        
        return response
    
    def handle_completion(self, message: str) -> str:
        """Handle task/goal completion"""
        msg_lower = message.lower()
        
        # Check for 'done' with a specific task mentioned
        done_match = re.search(r'done(?:\s+with)?\s+(.+)', msg_lower)
        
        # Get recent reminders and goals
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        # Get most recent reminder
        c.execute("""SELECT id, task FROM reminders 
                    WHERE phone_number = ? AND completed = 0
                    ORDER BY reminder_time LIMIT 1""",
                 (self.phone_number,))
        recent_reminder = c.fetchone()
        
        # Get most recent goal
        c.execute("""SELECT id, goal FROM goals 
                    WHERE phone_number = ? AND completed = 0
                    ORDER BY created_at DESC LIMIT 1""",
                 (self.phone_number,))
        recent_goal = c.fetchone()
        
        completed_something = False
        completed_item = ""
        
        # If they specified what they completed
        if done_match:
            task_desc = done_match.group(1).strip()
            # Try to match with reminders or goals
            # For now, we'll just acknowledge what they said
            completed_item = task_desc
            completed_something = True
        
        # If just "done" and there's a recent reminder
        elif recent_reminder and 'done' in msg_lower:
            reminder_id, task = recent_reminder
            c.execute("UPDATE reminders SET completed = 1 WHERE id = ?", (reminder_id,))
            completed_item = task
            completed_something = True
        
        # If it's about a goal
        elif recent_goal and any(word in msg_lower for word in ['done', 'completed', 'finished']):
            goal_id, goal_text = recent_goal
            c.execute("UPDATE goals SET completed = 1 WHERE id = ?", (goal_id,))
            completed_item = goal_text
            completed_something = True
        
        conn.commit()
        conn.close()
        
        if completed_something:
            congrats = [
                "🎉 Awesome job!",
                "🌟 Way to go!",
                "💪 You did it!",
                "🔥 Nice work!",
                "✨ Fantastic!",
                "🎊 Great job!",
                "👏 Well done!"
            ]
            
            motivations = [
                "You're on fire today!",
                "Keep up the great work!",
                "You're crushing it!",
                "That's how it's done!",
                "You're making great progress!",
                "Look at you go!",
                "You're unstoppable!"
            ]
            
            if completed_item:
                response = f"{random.choice(congrats)} You completed: {completed_item}\n\n{random.choice(motivations)}"
            else:
                response = f"{random.choice(congrats)} {random.choice(motivations)}"
            
            response += "\n\nWhat's next? 😊"
            return response
        else:
            return """That's great! What did you complete? 

You can say:
• "Done" (marks your most recent task)
• "Done with [task name]"
• "Finished my goal"

Or tell me what you accomplished and I'll celebrate with you! 🎉"""
    
    def smart_fallback(self, message: str) -> str:
        """Smart responses for unmatched patterns"""
        msg_lower = message.lower()
        
        # Check for question words
        if any(word in msg_lower.split()[0:2] for word in ['what', 'why', 'how', 'when', 'where', 'who']):
            if USE_AI:
                return self.process_with_ai(message)  # Use AI for questions
            else:
                return f"""That's a great question! While I can't answer complex questions without AI enabled, I can help you with:

📅 Setting reminders
🎯 Tracking goals
🍳 Finding recipes
⏰ Time management
✏️ You can also name me!

For deeper questions, you might want to:
• Google it for quick facts
• Ask a knowledgeable friend
• Research from reliable sources

How else can I assist you today?"""
        
        # Emotional support
        elif any(word in msg_lower for word in ['stressed', 'tired', 'sad', 'angry', 'frustrated']):
            return """I hear you're going through a tough time. 💙

Here are some things that might help:
• Take 5 deep breaths
• Go for a short walk
• Listen to calming music
• Talk to someone you trust
• Take a break from what's stressing you

Would you like me to:
• Set a reminder for a break?
• Help you break down what's overwhelming?
• Just listen while you share?

You've got this! 💪"""
        
        # Default helpful response
        else:
            return f"""I'm not sure how to help with that specific request, but I'm here for you!

I can definitely help with:
📅 Reminders - "Remind me to..."
🎯 Goals - "I want to..."
🍳 Recipes - "Recipe for..."
⏰ Planning - "What should I do?"
📊 Status - "Show my tasks"
✏️ Name me - "I'll call you..."

What would you like help with?"""
    
    def update_assistant_name(self, new_name: str):
        """Update assistant name in database"""
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        c.execute("UPDATE users SET assistant_name = ? WHERE phone_number = ?",
                 (new_name, self.phone_number))
        conn.commit()
        conn.close()
        self.user['assistant_name'] = new_name
    
    def save_conversation(self, role: str, content: str):
        """Save conversation to database"""
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        c.execute("""INSERT INTO conversations (phone_number, role, content, timestamp)
                    VALUES (?, ?, ?, ?)""",
                 (self.phone_number, role, content, datetime.now()))
        conn.commit()
        conn.close()
    
    def get_conversation_history(self, limit: int = 10) -> List[Dict]:
        """Get recent conversation history"""
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        c.execute("""SELECT role, content FROM conversations 
                    WHERE phone_number = ? 
                    ORDER BY timestamp DESC LIMIT ?""",
                 (self.phone_number, limit))
        
        history = []
        for row in reversed(c.fetchall()):
            history.append({"role": row[0], "content": row[1]})
        
        conn.close()
        return history
    
    def extract_and_execute_actions(self, message: str, ai_response: str):
        """Extract and execute actions from messages"""
        combined_text = message.lower() + " " + ai_response.lower()
        
        # Check for reminder intent
        if 'remind' in combined_text:
            reminder_time, task = self.parse_reminder(message)
            if reminder_time and task:
                job_id = f"reminder_{self.phone_number}_{reminder_time.timestamp()}"
                scheduler.add_job(
                    func=send_reminder,
                    trigger='date',
                    run_date=reminder_time,
                    args=[self.phone_number, task, self.user['assistant_name']],
                    id=job_id,
                    replace_existing=True
                )
        
        # Check for goal intent
        if any(phrase in combined_text for phrase in ['i will', 'i want to', 'my goal']):
            # Extract goal from original message
            goal_text = message
            for prefix in ['i want to', 'i will', 'my goal is']:
                goal_text = goal_text.lower().replace(prefix, '')
            goal_text = goal_text.strip()
            
            if len(goal_text) > 5:  # Reasonable goal length
                # Save goal
                conn = sqlite3.connect('assistant.db')
                c = conn.cursor()
                c.execute("""INSERT INTO goals (phone_number, goal, created_at)
                            VALUES (?, ?, ?)""",
                         (self.phone_number, goal_text, datetime.now()))
                conn.commit()
                conn.close()

# Helper functions for scheduled jobs
def send_reminder(phone_number: str, task: str, assistant_name: str):
    """Send a reminder message"""
    # Make it conversational with variety
    intros = [
        f"Hey! Just reminding you to {task} 😊",
        f"Hi there! Time to {task}",
        f"Hey, it's {assistant_name}! Don't forget to {task}",
        f"Quick reminder: {task} 📌",
        f"Heads up! You wanted me to remind you to {task}",
        f"Hey friend! It's time to {task}",
        f"Pssst... remember to {task}! ⏰"
    ]
    
    outros = [
        "\n\nLet me know when you're done!",
        "\n\nReply 'done' when you've finished!",
        "\n\nYou've got this! 💪",
        "\n\nHope this helps! Reply if you need anything.",
        "\n\nGood luck! Let me know how it goes.",
        "\n\nI'm here if you need help!"
    ]
    
    message = random.choice(intros) + random.choice(outros)
    
    try:
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number
        )
        logger.info(f"Sent reminder to {phone_number}: {task}")
    except Exception as e:
        logger.error(f"Failed to send reminder: {e}")

def send_goal_checkin(phone_number: str, goal: str, assistant_name: str, check_number: int):
    """Send a goal check-in message"""
    # More casual and varied check-ins
    check_ins = {
        1: [
            f"Hey! How's it going with your goal to {goal}? 😊",
            f"Quick check - any progress on {goal}?",
            f"Hi! Just wanted to see how {goal} is coming along!",
            f"Hey there! Made any headway on {goal}?"
        ],
        2: [
            f"Checking in again! How's {goal} going? Halfway there! 🎯",
            f"Hey! Still working on {goal}? You're doing great!",
            f"Mid-day check: how's the progress on {goal}?",
            f"Hi! Hope {goal} is going well. Need any help?"
        ],
        3: [
            f"Final check for today! How did {goal} go? 🌟",
            f"Hey! Wrapping up the day - how far did you get with {goal}?",
            f"Evening check-in: did you make progress on {goal}?",
            f"Hi! How did it go with {goal} today?"
        ]
    }
    
    messages = check_ins.get(check_number, check_ins[1])
    
    message = random.choice(messages) + "\n\nNo pressure - just let me know how you're doing! 💪"
    
    try:
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number
        )
        logger.info(f"Sent goal check-in to {phone_number} for: {goal}")
    except Exception as e:
        logger.error(f"Failed to send goal check-in: {e}")

# Morning briefing job
def send_morning_briefings():
    """Send morning briefings to users who want them"""
    conn = sqlite3.connect('assistant.db')
    c = conn.cursor()
    
    # Get users who have morning briefings enabled
    c.execute("""SELECT DISTINCT u.phone_number, u.assistant_name, u.timezone 
                FROM users u
                JOIN conversations c ON u.phone_number = c.phone_number
                WHERE c.timestamp > datetime('now', '-7 days')""")
    
    active_users = c.fetchall()
    conn.close()
    
    for phone_number, assistant_name, timezone in active_users:
        try:
            tz = pytz.timezone(timezone)
            current_hour = datetime.now(tz).hour
            
            # Send only if it's morning time (7-9 AM) in user's timezone
            if 7 <= current_hour <= 9:
                assistant = PersonalAssistant(phone_number)
                briefing = assistant.morning_routine()
                
                twilio_client.messages.create(
                    body=briefing,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=phone_number
                )
                
                logger.info(f"Sent morning briefing to {phone_number}")
        except Exception as e:
            logger.error(f"Error sending morning briefing to {phone_number}: {e}")

# Schedule morning briefings
scheduler.add_job(
    func=send_morning_briefings,
    trigger='cron',
    hour='7-9',
    minute=0,
    id='morning_briefings'
)

@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        
        logger.info(f"Received from {from_number}: {incoming_msg}")
        
        # Initialize assistant for this user
        assistant = PersonalAssistant(from_number)
        
        # Process message
        response = assistant.process_message(incoming_msg)
        
        # Send response
        if response:
            # Handle long messages
            if len(response) > 1600:
                # Split into chunks
                chunks = []
                current_chunk = ""
                
                for line in response.split('\n'):
                    if len(current_chunk) + len(line) + 1 < 1600:
                        current_chunk += line + '\n'
                    else:
                        chunks.append(current_chunk.strip())
                        current_chunk = line + '\n'
                
                if current_chunk:
                    chunks.append(current_chunk.strip())
                
                # Send chunks
                for i, chunk in enumerate(chunks):
                    twilio_client.messages.create(
                        body=chunk + ("\n\n[continued...]" if i < len(chunks) - 1 else ""),
                        from_=TWILIO_WHATSAPP_NUMBER,
                        to=from_number
                    )
            else:
                twilio_client.messages.create(
                    body=response,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to=from_number
                )
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        # Send error message to user
        try:
            twilio_client.messages.create(
                body="Oops! Something went wrong. Please try again or say 'help' for assistance.",
                from_=TWILIO_WHATSAPP_NUMBER,
                to=from_number
            )
        except:
            pass
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        # Check database connection
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        user_count = c.fetchone()[0]
        conn.close()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'total_users': user_count,
            'ai_enabled': USE_AI,
            'scheduler_running': scheduler.running
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500

@app.route('/')
def home():
    """Home page"""
    return f"""
    <h1>🤖 WhatsApp Personal Assistant</h1>
    <h2>Status: ✅ Running</h2>
    
    <h3>Features:</h3>
    <ul>
        <li>📅 Smart reminders with timezone detection</li>
        <li>🎯 Goal tracking with intelligent check-ins</li>
        <li>🍳 Recipe search from internet</li>
        <li>⏰ Time management suggestions</li>
        <li>🧠 AI-powered responses ({USE_AI and 'Enabled' or 'Disabled'})</li>
        <li>💬 Natural language understanding</li>
        <li>✏️ Custom assistant naming</li>
    </ul>
    
    <h3>How to use:</h3>
    <ol>
        <li>Save the Twilio WhatsApp number</li>
        <li>Send "Hi" to get started</li>
        <li>Name your assistant: "I'll call you Jarvis"</li>
        <li>The bot adapts to your timezone automatically</li>
    </ol>
    
    <p><strong>AI Mode:</strong> {'Enabled with OpenAI' if USE_AI else 'Disabled (using smart patterns)'}</p>
    <p><strong>Server Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    
    print("\n" + "="*50)
    print("🚀 WhatsApp Personal Assistant Starting...")
    print("="*50)
    print(f"✅ Timezone detection: Enabled")
    print(f"✅ Smart reminders: Active")
    print(f"✅ Goal tracking: Active")
    print(f"✅ Recipe search: Active")
    print(f"✅ AI Mode: {'Enabled (OpenAI connected)' if USE_AI else 'Disabled (using smart patterns)'}")
    print(f"✅ Database: SQLite initialized")
    print(f"✅ Scheduler: Running")
    print(f"\n🌐 Server running at: http://localhost:{port}")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=port, debug=False)