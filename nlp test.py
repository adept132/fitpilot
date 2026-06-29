import spacy

nlp = spacy.load("ru_core_news_sm")

def preprocess_text(user_input):
    doc = nlp(user_input)
    tokens = [token.lemma_.lower() for token in doc if not token.is_stop and not token.is_punct]

    return tokens

user_input = "Привет, как дела? Я люблю заниматься спортом!"
processed_text = preprocess_text(user_input)
print(processed_text)