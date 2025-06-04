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
                'assistant_name': user[2],
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
        msg_lower = message.lower()
        current_time = datetime.now(self.timezone)
        
        # Greeting
        if any(word in msg_lower.split() for word in ['hi', 'hello', 'hey', 'start', 'help']):
            return f"""👋 Hi! I'm {self.user['assistant_name']}, your personal assistant!

I can help you:
📅 Set reminders - "Remind me to call mom at 5 PM"
🎯 Track goals - "I want to write 1500 words today"
🍳 Find recipes - "Show me a pasta recipe"  
⏰ Manage your time - "What should I do now?"
📝 Stay organized - "My tasks for today are..."

Your timezone: {self.user['timezone']} ({current_time.strftime('%I:%M %p')})

What would you like help with?"""

        # Set assistant name
        elif 'your name is' in msg_lower or 'call you' in msg_lower:
            match = re.search(r'(?:your name is|call you)\s+(\w+)', msg_lower)
            if match:
                new_name = match.group(1).capitalize()
                self.update_assistant_name(new_name)
                return f"Great! I'm {new_name} now. How can I help you today? 😊"

        # Reminders
        elif 'remind' in msg_lower:
            return self.handle_reminder(message)

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

        # Status check
        elif 'status' in msg_lower or 'pending' in msg_lower or 'my tasks' in msg_lower:
            return self.get_status()

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
                id=job_id
            )
            
            # Save to database
            conn = sqlite3.connect('assistant.db')
            c = conn.cursor()
            c.execute("""INSERT INTO reminders (phone_number, task, reminder_time, created_at)
                        VALUES (?, ?, ?, ?)""",
                     (self.phone_number, task, reminder_time, datetime.now()))
            conn.commit()
            conn.close()
            
            time_str = reminder_time.strftime('%I:%M %p')
            date_str = "today" if reminder_time.date() == datetime.now(self.timezone).date() else reminder_time.strftime('%B %d')
            
            return f"""✅ Reminder set!

📌 Task: {task}
⏰ Time: {time_str} {date_str}

I'll message you then! 

💡 Tip: You can say "show my reminders" to see all pending reminders."""
        else:
            return """I need more details to set a reminder. Try:
• "Remind me to call mom at 5 PM"
• "Remind me in 2 hours to take medicine"
• "Remind me tomorrow at 9 AM to submit report"

What would you like me to remind you about?"""
    
    def handle_goal(self, message: str) -> str:
        """Handle goal setting with smart check-ins"""
        # Extract the goal
        goal_text = message
        
        # Clean up common prefixes
        for prefix in ['i want to', 'i need to', 'my goal is to', 'i have to', 'i must']:
            goal_text = goal_text.lower().replace(prefix, '')
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
                id=job_id
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
        
        response = f"📊 **Your Current Status**\n\n"
        
        if reminders:
            response += "⏰ **Upcoming Reminders:**\n"
            for task, time_str in reminders:
                reminder_time = datetime.fromisoformat(time_str)
                time_until = reminder_time - datetime.now()
                
                if time_until.days > 0:
                    time_desc = f"in {time_until.days} days"
                elif time_until.total_seconds() / 3600 > 1:
                    time_desc = f"in {int(time_until.total_seconds() / 3600)} hours"
                else:
                    time_desc = f"in {int(time_until.total_seconds() / 60)} minutes"
                
                response += f"• {task} ({time_desc})\n"
        else:
            response += "⏰ No pending reminders\n"
        
        response += "\n"
        
        if goals:
            response += "🎯 **Active Goals:**\n"
            for goal, created in goals:
                created_date = datetime.fromisoformat(created).date()
                days_active = (datetime.now().date() - created_date).days
                response += f"• {goal} (Day {days_active + 1})\n"
        else:
            response += "🎯 No active goals\n"
        
        response += "\n💡 Reply 'done' when you complete something!"
        
        return response
    
    def handle_completion(self, message: str) -> str:
        """Handle task/goal completion"""
        # Get most recent active goal
        conn = sqlite3.connect('assistant.db')
        c = conn.cursor()
        
        c.execute("""SELECT id, goal FROM goals 
                    WHERE phone_number = ? AND completed = 0
                    ORDER BY created_at DESC LIMIT 1""",
                 (self.phone_number,))
        recent_goal = c.fetchone()
        
        if recent_goal:
            goal_id, goal_text = recent_goal
            c.execute("UPDATE goals SET completed = 1 WHERE id = ?", (goal_id,))
            conn.commit()
            conn.close()
            
            congrats = random.choice([
                "🎉 Amazing work!",
                "🌟 Fantastic job!",
                "💪 You crushed it!",
                "🔥 Excellent work!",
                "✨ Well done!"
            ])
            
            return f"""{congrats} You completed: {goal_text}

You're making great progress! What's next on your list?

💡 Tip: Celebrating small wins builds momentum for bigger achievements!"""
        else:
            conn.close()
            return "Great job completing that! What did you finish? I'd love to celebrate with you! 🎉"
    
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

What would you like help with?"""
    
    def parse_reminder(self, text: str) -> Tuple[Optional[datetime], Optional[str]]:
        """Parse reminder time and task from text"""
        text_lower = text.lower()
        now = datetime.now(self.timezone)
        
        # Time patterns
        time_match = re.search(r'(\d{1,2})\s*(am|pm|AM|PM)', text)
        relative_match = re.search(r'in\s+(\d+)\s*(hour|minute|min|hr)', text_lower)
        
        reminder_time = None
        
        # Check for relative time
        if relative_match:
            amount = int(relative_match.group(1))
            unit = relative_match.group(2).lower()
            
            if 'hour' in unit:
                reminder_time = now + timedelta(hours=amount)
            else:  # minutes
                reminder_time = now + timedelta(minutes=amount)
        
        # Check for specific time
        elif time_match:
            hour = int(time_match.group(1))
            period = time_match.group(2).lower()
            
            if period == 'pm' and hour != 12:
                hour += 12
            elif period == 'am' and hour == 12:
                hour = 0
            
            reminder_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            
            # Check for tomorrow
            if 'tomorrow' in text_lower:
                reminder_time += timedelta(days=1)
            elif reminder_time <= now:
                # If time has passed today, assume tomorrow
                reminder_time += timedelta(days=1)
        
        # Extract task by removing time-related words
        if reminder_time:
            task = text
            time_words = ['remind', 'me', 'to', 'at', 'in', 'tomorrow', 'am', 'pm', 
                         'hour', 'hours', 'minute', 'minutes', 'min', 'hr']
            
            for word in time_words:
                task = re.sub(r'\b' + word + r's?\b', '', task, flags=re.IGNORECASE)
            
            # Clean up numbers that are part of time
            if time_match:
                task = task.replace(time_match.group(0), '')
            if relative_match:
                task = task.replace(relative_match.group(0), '')
            
            task = ' '.join(task.split()).strip()
            
            if not task:
                task = "Reminder"
            
            return reminder_time, task
        
        return None, None
    
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
                    id=job_id
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
    message = f"""🔔 **{assistant_name} Reminder**

{task}

Reply 'done' when completed or 'snooze' to delay 15 minutes."""
    
    twilio_client.messages.create(
        body=message,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=phone_number
    )
    
    logger.info(f"Sent reminder to {phone_number}: {task}")

def send_goal_checkin(phone_number: str, goal: str, assistant_name: str, check_number: int):
    """Send a goal check-in message"""
    messages = [
        f"How's progress on: {goal}? Even small steps count! 💪",
        f"Checking in on your goal: {goal}. How's it going? 🎯",
        f"Time for a progress check! How are you doing with: {goal}? 🌟",
        f"Quick check: Any updates on {goal}? You've got this! 🔥"
    ]
    
    message = f"""📊 **{assistant_name} Check-in #{check_number}**

{random.choice(messages)}

Reply with:
• Your progress so far
• 'done' if completed
• 'help' if you're stuck

Remember: Progress > Perfection! 🚀"""
    
    twilio_client.messages.create(
        body=message,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=phone_number
    )
    
    logger.info(f"Sent goal check-in to {phone_number} for: {goal}")

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
    </ul>
    
    <h3>How to use:</h3>
    <ol>
        <li>Save the Twilio WhatsApp number</li>
        <li>Send "Hi" to get started</li>
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