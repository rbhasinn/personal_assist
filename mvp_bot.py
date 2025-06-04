import os
import json
import pytz
import logging
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from dotenv import load_dotenv
import pickle
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import re

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize scheduler with memory store (no Redis needed for MVP)
jobstores = {
    'default': MemoryJobStore()
}
scheduler = BackgroundScheduler(jobstores=jobstores, timezone='Asia/Kolkata')
scheduler.start()

# Twilio configuration
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_API_KEY = os.getenv('TWILIO_API_KEY')
TWILIO_API_SECRET = os.getenv('TWILIO_API_SECRET')
TWILIO_WHATSAPP_NUMBER = os.getenv('TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886')

# Use API Key authentication instead of Auth Token
twilio_client = Client(TWILIO_API_KEY, TWILIO_API_SECRET, account_sid=TWILIO_ACCOUNT_SID)

# In-memory storage for MVP (replace with database later)
users = {}
reminders = {}
daily_goals = {}

# Simple recipe search using public API
def search_recipes_online(query):
    """Search recipes from TheMealDB (free API)"""
    try:
        # Search by main ingredient
        response = requests.get(f'https://www.themealdb.com/api/json/v1/1/filter.php?i={query}')
        if response.status_code == 200:
            data = response.json()
            meals = data.get('meals', [])
            if meals:
                # Get details of first meal
                meal_id = meals[0]['idMeal']
                detail_response = requests.get(f'https://www.themealdb.com/api/json/v1/1/lookup.php?i={meal_id}')
                if detail_response.status_code == 200:
                    meal_data = detail_response.json()['meals'][0]
                    return format_recipe(meal_data)
        
        # Fallback to search by name
        response = requests.get(f'https://www.themealdb.com/api/json/v1/1/search.php?s={query}')
        if response.status_code == 200:
            data = response.json()
            meals = data.get('meals', [])
            if meals:
                return format_recipe(meals[0])
    except Exception as e:
        logger.error(f"Recipe search error: {e}")
    
    return None

def format_recipe(meal_data):
    """Format recipe data for WhatsApp"""
    name = meal_data.get('strMeal', 'Recipe')
    category = meal_data.get('strCategory', '')
    instructions = meal_data.get('strInstructions', '')
    
    # Get ingredients
    ingredients = []
    for i in range(1, 21):
        ingredient = meal_data.get(f'strIngredient{i}')
        measure = meal_data.get(f'strMeasure{i}')
        if ingredient and ingredient.strip():
            ingredients.append(f"• {measure} {ingredient}".strip())
    
    # Format message
    message = f"""🍳 *{name}*
📂 Category: {category}

📝 *Ingredients:*
{chr(10).join(ingredients[:10])}  # Limit to 10 ingredients

👨‍🍳 *Instructions:*
{instructions[:500]}...  # Limit length

🔗 More recipes? Just ask for another dish!"""
    
    return message

def get_user_data(phone_number):
    """Get or create user data"""
    if phone_number not in users:
        users[phone_number] = {
            'name': 'Friend',
            'assistant_name': 'Assistant',
            'timezone': 'Asia/Kolkata',
            'calendar_connected': False,
            'created_at': datetime.now().isoformat()
        }
    return users[phone_number]

def send_reminder(phone_number, task, reminder_id):
    """Send reminder via WhatsApp"""
    try:
        user = get_user_data(phone_number)
        assistant_name = user.get('assistant_name', 'Assistant')
        
        message = f"""🔔 *Reminder from {assistant_name}*

📌 {task}

Reply 'done' to mark as complete or 'snooze' to delay by 30 mins."""
        
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number
        )
        
        # Remove from active reminders
        if reminder_id in reminders:
            del reminders[reminder_id]
            
    except Exception as e:
        logger.error(f"Error sending reminder: {e}")

def schedule_reminder(phone_number, task, reminder_time):
    """Schedule a reminder"""
    reminder_id = f"reminder_{phone_number}_{reminder_time.timestamp()}"
    
    # Schedule the job
    scheduler.add_job(
        func=send_reminder,
        trigger='date',
        run_date=reminder_time,
        args=[phone_number, task, reminder_id],
        id=reminder_id
    )
    
    # Store reminder info
    reminders[reminder_id] = {
        'phone_number': phone_number,
        'task': task,
        'time': reminder_time.isoformat()
    }
    
    return reminder_id

def parse_reminder_time(text):
    """Parse time from message"""
    # Time patterns
    time_match = re.search(r'(\d{1,2})\s*(am|pm|AM|PM)', text)
    
    if time_match:
        hour = int(time_match.group(1))
        period = time_match.group(2).lower()
        
        if period == 'pm' and hour != 12:
            hour += 12
        elif period == 'am' and hour == 12:
            hour = 0
        
        # Check for tomorrow
        ist = pytz.timezone('Asia/Kolkata')
        reminder_time = datetime.now(ist).replace(hour=hour, minute=0, second=0, microsecond=0)
        
        if 'tomorrow' in text.lower():
            reminder_time += timedelta(days=1)
        elif reminder_time <= datetime.now(ist):
            # If time has passed today, assume tomorrow
            reminder_time += timedelta(days=1)
        
        return reminder_time
    
    # Handle relative times
    if 'in' in text.lower():
        minutes_match = re.search(r'in (\d+) min', text.lower())
        hours_match = re.search(r'in (\d+) hour', text.lower())
        
        ist = pytz.timezone('Asia/Kolkata')
        if minutes_match:
            minutes = int(minutes_match.group(1))
            return datetime.now(ist) + timedelta(minutes=minutes)
        elif hours_match:
            hours = int(hours_match.group(1))
            return datetime.now(ist) + timedelta(hours=hours)
    
    return None

def schedule_daily_checkins(phone_number, goals):
    """Schedule periodic check-ins for daily goals"""
    # Schedule check-ins at 2PM, 5PM, and 8PM
    checkin_times = [14, 17, 20]  # 2 PM, 5 PM, 8 PM
    
    ist = pytz.timezone('Asia/Kolkata')
    today = datetime.now(ist).date()
    
    for hour in checkin_times:
        checkin_time = ist.localize(datetime.combine(today, datetime.min.time().replace(hour=hour)))
        
        if checkin_time > datetime.now(ist):
            job_id = f"checkin_{phone_number}_{checkin_time.timestamp()}"
            
            scheduler.add_job(
                func=send_goal_checkin,
                trigger='date',
                run_date=checkin_time,
                args=[phone_number, goals],
                id=job_id
            )

def send_goal_checkin(phone_number, goals):
    """Send check-in message for daily goals"""
    user = get_user_data(phone_number)
    assistant_name = user.get('assistant_name', 'Assistant')
    
    # Get incomplete goals
    user_goals = daily_goals.get(phone_number, {})
    pending = [g for g, status in user_goals.items() if not status['completed']]
    
    if pending:
        message = f"""👋 *{assistant_name} checking in!*

How's progress on your goals?

📋 *Pending tasks:*
{chr(10).join(['• ' + g for g in pending[:5]])}

Reply with what you've completed or 'all done' if finished!"""
        
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=phone_number
        )

def get_calendar_events():
    """Get today's events from Google Calendar"""
    try:
        # Load credentials
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
            
            service = build('calendar', 'v3', credentials=creds)
            
            # Get today's events
            ist = pytz.timezone('Asia/Kolkata')
            today_start = datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)
            
            events_result = service.events().list(
                calendarId='primary',
                timeMin=today_start.isoformat(),
                timeMax=today_end.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            return events_result.get('items', [])
    except Exception as e:
        logger.error(f"Calendar error: {e}")
        return []

def send_morning_briefing(phone_number):
    """Send morning briefing with calendar and ask for daily goals"""
    user = get_user_data(phone_number)
    assistant_name = user.get('assistant_name', 'Assistant')
    
    # Get calendar events
    events = get_calendar_events()
    
    # Format schedule
    schedule_text = "No meetings today! 🎉"
    if events:
        schedule_lines = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            if 'T' in start:
                time = datetime.fromisoformat(start.replace('Z', '+00:00'))
                time_str = time.strftime('%I:%M %p')
                title = event.get('summary', 'No title')
                schedule_lines.append(f"• {time_str} - {title}")
        schedule_text = '\n'.join(schedule_lines[:5])  # Limit to 5 events
    
    message = f"""🌅 *Good morning! {assistant_name} here*

📅 *Today's Schedule:*
{schedule_text}

💭 *What do you want to accomplish today?*
Tell me your goals and I'll check in periodically to help you stay on track!

You can say things like:
• "Study chapters 3 and 4"
• "Complete project report"
• "Workout for 30 mins"
• "Call 5 clients"

*Send voice note or text with your goals!*"""
    
    twilio_client.messages.create(
        body=message,
        from_=TWILIO_WHATSAPP_NUMBER,
        to=phone_number
    )

@app.route('/webhook', methods=['POST'])
def whatsapp_webhook():
    """Handle incoming WhatsApp messages"""
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        
        logger.info(f"Received from {from_number}: {incoming_msg}")
        
        # Get user data
        user = get_user_data(from_number)
        msg_lower = incoming_msg.lower()
        
        # Handle different commands
        if any(word in msg_lower for word in ['hi', 'hello', 'hey', 'start']):
            response = f"""🙏 *Welcome! I'm {user['assistant_name']}*

I'm your personal WhatsApp assistant that actually works!

*What I can do:*
📅 Set real reminders that message you
🎯 Track your daily goals with check-ins
🍳 Search recipes from the internet
📆 Show your calendar (if connected)
✨ Remember your preferences

*Try these commands:*
• "Remind me to call mom at 5 PM"
• "Find recipe for pasta"
• "My goals today are..."
• "Your name is Jarvis"

*Want to set up morning briefings?* Say "Enable morning briefings"

What would you like to do?"""
        
        elif 'your name is' in msg_lower:
            match = re.search(r'your name is (\w+)', msg_lower)
            if match:
                new_name = match.group(1).capitalize()
                user['assistant_name'] = new_name
                response = f"😊 Perfect! I'm now {new_name}, your personal assistant. How can I help you today?"
        
        elif 'remind' in msg_lower:
            reminder_time = parse_reminder_time(incoming_msg)
            if reminder_time:
                # Extract task
                task = incoming_msg.lower()
                for word in ['remind', 'me', 'to', 'at', 'am', 'pm', 'tomorrow']:
                    task = task.replace(word, ' ')
                task = ' '.join(task.split()).strip()
                
                if not task:
                    task = "Reminder"
                
                # Schedule it
                schedule_reminder(from_number, task.title(), reminder_time)
                
                response = f"""✅ *Reminder Set!*
📌 Task: {task.title()}
⏰ Time: {reminder_time.strftime('%I:%M %p')}
📅 Date: {reminder_time.strftime('%B %d, %Y')}

I'll message you at the scheduled time!"""
            else:
                response = "Please specify a time. Examples:\n• Remind me to exercise at 6 PM\n• Remind me in 30 minutes to take medicine"
        
        elif 'recipe' in msg_lower or 'cook' in msg_lower or 'food' in msg_lower:
            # Extract dish name
            query = incoming_msg.lower()
            for word in ['recipe', 'for', 'cook', 'make', 'find']:
                query = query.replace(word, ' ')
            query = ' '.join(query.split()).strip()
            
            if not query:
                response = "What would you like to cook? Try: 'Recipe for chicken curry' or 'Recipe for pasta'"
            else:
                # Search online
                recipe = search_recipes_online(query)
                if recipe:
                    response = recipe
                else:
                    response = f"Sorry, couldn't find a recipe for '{query}'. Try another dish or be more specific!"
        
        elif 'my goals' in msg_lower or 'today i want to' in msg_lower:
            # Extract goals
            goals_text = incoming_msg.lower()
            for phrase in ['my goals today are', 'my goals are', 'today i want to', 'i want to']:
                goals_text = goals_text.replace(phrase, '')
            
            # Split by common separators
            import re
            goals = re.split(r'[,;]|and|then', goals_text)
            goals = [g.strip() for g in goals if g.strip()]
            
            if goals:
                # Store goals
                daily_goals[from_number] = {goal: {'completed': False, 'added_at': datetime.now()} for goal in goals}
                
                # Schedule check-ins
                schedule_daily_checkins(from_number, goals)
                
                response = f"""🎯 *Goals Set for Today!*

I'll track these for you:
{chr(10).join(['✓ ' + g.title() for g in goals[:10]])}

📱 I'll check in at:
• 2:00 PM
• 5:00 PM
• 8:00 PM

To help you stay on track! You can update me anytime by saying "completed [task]" or "all done"!"""
            else:
                response = "Please tell me your goals. Example: 'My goals today are study math, exercise, and finish report'"
        
        elif 'completed' in msg_lower or 'done' in msg_lower or 'finished' in msg_lower:
            if from_number in daily_goals:
                if 'all done' in msg_lower:
                    # Mark all as complete
                    for goal in daily_goals[from_number]:
                        daily_goals[from_number][goal]['completed'] = True
                    response = "🎉 Amazing! You've completed all your goals for today! Well done! 🌟"
                else:
                    # Mark specific goal as complete
                    completed_any = False
                    for goal in daily_goals[from_number]:
                        if goal.lower() in msg_lower:
                            daily_goals[from_number][goal]['completed'] = True
                            completed_any = True
                    
                    if completed_any:
                        pending = [g for g, status in daily_goals[from_number].items() if not status['completed']]
                        if pending:
                            response = f"✅ Great progress!\n\nStill pending:\n{chr(10).join(['• ' + g.title() for g in pending])}"
                        else:
                            response = "🎉 All goals completed! You're on fire today! 🔥"
                    else:
                        response = "Which task did you complete? Please be specific or say 'all done'"
            else:
                response = "You haven't set any goals today. Say 'My goals today are...' to get started!"
        
        elif 'morning briefing' in msg_lower:
            if 'enable' in msg_lower or 'start' in msg_lower or 'yes' in msg_lower:
                # Schedule morning briefing for tomorrow
                ist = pytz.timezone('Asia/Kolkata')
                tomorrow = datetime.now(ist).date() + timedelta(days=1)
                briefing_time = ist.localize(datetime.combine(tomorrow, datetime.min.time().replace(hour=7)))
                
                job_id = f"morning_{from_number}_{briefing_time.timestamp()}"
                scheduler.add_job(
                    func=send_morning_briefing,
                    trigger='date',
                    run_date=briefing_time,
                    args=[from_number],
                    id=job_id
                )
                
                response = "☀️ Morning briefings enabled! I'll message you tomorrow at 7 AM with your schedule and ask for your daily goals."
            else:
                response = "Would you like me to send you a morning briefing every day at 7 AM? Say 'Enable morning briefings' to start!"
        
        elif 'help' in msg_lower:
            response = """📚 *Help Menu*

*Reminders:*
• Remind me to [task] at [time]
• Remind me in 30 minutes to [task]

*Goals:*
• My goals today are [goal1], [goal2]
• Completed [specific task]
• All done

*Recipes:*
• Recipe for [dish name]
• Find pasta recipe

*Calendar:*
• What's my schedule
• Enable morning briefings

*Personalization:*
• Your name is [name]

*Examples:*
• "Remind me to take medicine at 9 PM"
• "My goals today are read 2 chapters and exercise"
• "Recipe for chicken biryani"

What would you like help with?"""
        
        else:
            response = f"I didn't understand that. Say 'help' to see what I can do, or try:\n• Set a reminder\n• Search for a recipe\n• Set daily goals"
        
        # Send response
        message = twilio_client.messages.create(
            body=response,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=from_number
        )
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'active_reminders': len(reminders),
        'active_users': len(users)
    }), 200

@app.route('/')
def home():
    """Home page"""
    return """
    <h1>WhatsApp Assistant MVP</h1>
    <p>Bot is running!</p>
    <p>Active reminders: {}</p>
    <p>Active users: {}</p>
    """.format(len(reminders), len(users))

if __name__ == '__main__':
    print("\n🚀 WhatsApp Assistant MVP Starting...")
    print("✅ Features:")
    print("  - Real working reminders")
    print("  - Daily goal tracking with check-ins")
    print("  - Internet recipe search")
    print("  - Calendar integration (if configured)")
    print("  - Persistent throughout the day")
    print("\nMake sure ngrok is running and webhook is configured!")
    print("Bot running at: http://localhost:8080\n")
    
    app.run(host='0.0.0.0', port=5555, debug=False)