import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, classification_report
from sklearn.preprocessing import StandardScaler
from sklearn.utils.class_weight import compute_class_weight
import tensorflow as tf
from tensorflow.keras import layers, models
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import TensorDataset, DataLoader
import random
import numpy as np

# Read in the data
matches = pd.read_csv("matches_5.csv", index_col=0)
cons_win = [1, 3, 5, 7, 10]

# Clean/convert the data
matches["date"] = pd.to_datetime(matches["Date"])
matches.drop("Date", axis=1, inplace=True)
matches["venue_num"] = matches["Venue"].astype("category").cat.codes
matches["opp_num"] = matches["Opponent"].astype("category").cat.codes
matches["hour"] = matches["Time"].str.replace(":.+", "", regex=True).astype("int")
matches["day_num"] = matches["date"].dt.day_of_week
matches["poss_xg"] = matches["Poss"] * matches["xG"]
matches["shot_ef"] = matches["SoT"] / matches["Sh"]
matches["xg_dif"] = matches["xG"] - matches["xGA"]

def res_pts(result):
    '''
    Returns 3 if the result is a win, 1 if it is a draw, or 0 if it is a loss
    '''
    if result == 'W':
        return 3
    elif result == 'D':
        return 1
    else:
        return 0
    
# Convert match results to the number of points won
matches["points"] = matches["Result"].apply(res_pts)

# Define columns
cols = ["GF", "GA", "Sh", "SoT", "Dist", "FK", "PK", "PKatt", "xG", "xGA", "Poss", "shot_ef", "poss_xg", "xg_dif"]#, "npxG"]
roll_cols = [f"{c.lower()}_roll_{w}" for c in cols for w in cons_win]
predictors = ["venue_num", "opp_num", "hour", "day_num"]

# Compute rolling averages
def rolling_avg(group, col, new_col, window):
    '''
    Computes group's rolling average of the col columns and stores the values in the new_col columns
    '''
    group = group.sort_values("date")
    rolling = group[col].rolling(window, closed="left").mean()
    group[new_col] = rolling
    return group

def apply_rolling_averages(data, cols, windows=[3, 5, 10]):
    '''
    Applies the rolling_avg function to the data DataFrame's cols columns
    '''
    all_col = []
    res = data.copy()

    for w in windows:
        new_cols = [f"{c.lower()}_roll_{w}" for c in cols]
        temp = res.sort_values("date").groupby("Team", group_keys=False).apply(lambda x: rolling_avg(x, cols, new_cols, w)).reset_index(drop=True)

        res = temp
        all_col.extend(new_cols)
    return res, all_col

# Get the rolling averages
matches_roll, roll_cols = apply_rolling_averages(matches, cols, windows=cons_win)

# Split between training and testing data
train = matches_roll[matches_roll["date"] < "2025-01-01"].copy()
test = matches_roll[matches_roll["date"] >= "2025-01-01"].copy()

# Drop rows without rolling data
train = train.dropna(subset=roll_cols)
test = test.dropna(subset=roll_cols)

# Create a unique match ID for each game (based on date and both teams)
matches["match_id"] = matches.apply(lambda x: "_".join(sorted([x["Team"], x["Opponent"]]) + [x["date"].strftime("%Y-%m-%d")]), axis=1)
matches_roll["match_id"] = matches["match_id"].values

# Split into two: one from Team's perspective, one from Opponent's
team_cols = cols + roll_cols + ["Team", "Opponent", "Result", "points", "match_id", "date"]
# team1_df = matches[team_cols].copy()
# team2_df = matches[team_cols].copy()
team1_df = matches_roll[team_cols].copy()
team2_df = matches_roll[team_cols].copy()


# Rename columns to distinguish Team1 and Team2 stats
team1_df = team1_df.rename(columns={col: f"team1_{col}" for col in cols})
team2_df = team2_df.rename(columns={col: f"team2_{col}" for col in cols})
team1_df = team1_df.rename(columns={"Team": "Team1", "Opponent": "Team2", "Result": "result_team1", "points": "points_team1"})
team2_df = team2_df.rename(columns={"Team": "Team2", "Opponent": "Team1", "Result": "result_team2", "points": "points_team2"})

# Merge on match_id
combined = pd.merge(team1_df, team2_df, on="match_id", suffixes=("", "_opp"))

# Keep only one row per match (avoid duplicating perspectives)
combined = combined[combined["Team1"] < combined["Team2"]]  # Sort team names alphabetically for consistency
combined["target"] = combined["result_team1"].apply(res_pts)

# Now define new predictors
# Get all numeric feature columns for modeling
exclude_cols = ['Team1', 'Team2', 'Team1_opp', 'Team2_opp', 'match_id', 'date', 'date_opp', 'result_team1', 'points_team1', 'result_team2', 'points_team2', 'target']
new_predictors = [col for col in combined.columns if col not in exclude_cols and combined[col].dtype in ['int64', 'float64']]

# Drop rows with NaNs in any predictor
combined = combined.dropna(subset=new_predictors)

# Now re-check for issues
if combined[new_predictors].isnull().any().any():
    print("Still contains NaNs — something's wrong.")


if combined[new_predictors].isnull().any().any():
    print("Warning: NaNs in predictors")
    print(combined[new_predictors].isnull().sum())


# Scale features for TensorFlow / PyTorch
scaler = StandardScaler()
X = scaler.fit_transform(combined[new_predictors])
y = combined["target"].map({0: 0, 1: 1, 3: 2}).values  # Do this early and once

# Split into train/test sets
train_idx = combined["date"] < "2025-01-01"
test_idx  = combined["date"] > "2025-01-01"

X_train = X[train_idx]
X_test = X[test_idx]
y_train = y[train_idx]
y_test = y[test_idx]

# Ensuring no overlap between training and testing
leakage_cols = ["points", "result_team1", "result_team2", "target"]
combined = combined.drop(columns=[col for col in leakage_cols if col in combined.columns], errors='ignore')

# -----------------------------------------------------------------------------------
# Random Forest
# -----------------------------------------------------------------------------------

# Fitting data
rf = RandomForestClassifier(n_estimators=50, min_samples_split=10, random_state=1, class_weight="balanced")
#rf.fit(train[predictors + roll_cols], train["points"])
rf.fit(X_train, y_train)

# Making predictions
#test["prediction"] = rf.predict(test[predictors + roll_cols])
combined.loc[test_idx, "prediction"] = rf.predict(X_test)

# Evaluating prediction accuracy
print("\nRandom Forest Approach:\n")
# print("Accuracy:", accuracy_score(test["points"], test["prediction"]))
# print("Accuracy:", accuracy_score(y_test, test["prediction"]))
# print("\nConfusion Matrix:\n", confusion_matrix(test["points"], test["prediction"]))
# print("\nClassification Report:\n", classification_report(test["points"], test["prediction"]))

print("Accuracy:", accuracy_score(y_test, combined.loc[test_idx, "prediction"]))
print("\nConfusion Matrix:\n", confusion_matrix(y_test, combined.loc[test_idx, "prediction"]))
print("\nClassification Report:\n", classification_report(y_test, combined.loc[test_idx, "prediction"]))


# Predict probabilities for each class
# probs = rf.predict_proba(test[predictors + roll_cols])
probs = rf.predict_proba(X_test)

# Classes might not be in order [0, 1, 3] ==> map manually
# Get class order from the trained model
class_order = rf.classes_

# Create a mapping of class index to point value
expected_points = sum(probs[:, i] * class_order[i] for i in range(len(class_order)))
# test["expected_points"] = expected_points
combined.loc[test_idx, "expected_points"] = expected_points

# Show Sample Predictions
# print("\nSample predictions:")
# print(test[["Team", "Opponent", "date", "Result", "points", "prediction", "expected_points"]].head(10))

# -----------------------------------------------------------------------------------
# Tensor Flow
# -----------------------------------------------------------------------------------

# Making deterministic seeds
tf.random.set_seed(42)
np.random.seed(42)
random.seed(42)

# Mapping points to indices
# points_map = {0: 0, 1: 1, 3: 2}
# train["target"] = train["points"].map(points_map)
# test["target"] = test["points"].map(points_map)

# Normalize the inputs
scaler = StandardScaler()
# X_train = scaler.fit_transform(train[predictors + roll_cols])
# X_test = scaler.transform(test[predictors + roll_cols])

# y_train = train["target"].values
# y_test = test["target"].values

# Build and train neural networks
model = models.Sequential([
    layers.Input(shape=(X_train.shape[1],)),
    layers.Dense(128, activation='relu'),
    layers.Dropout(0.3),
    layers.Dense(64, activation='relu'),
    layers.Dense(3, activation='softmax')
])

model.compile(
    optimizer='adam',
    loss='sparse_categorical_crossentropy',
    metrics=['accuracy']
)

class_w = compute_class_weight(class_weight='balanced', classes=np.unique(y_train), y=y_train)
w0, w1, w2 = class_w
model.fit(X_train, y_train, epochs=30, batch_size=16, validation_split=0.2, class_weight={0: w0, 1: w1, 2: w2})

# Evaluate model accuracy
print("Tensor Flow Neural Network Approach:")
y_pred = model.predict(X_test).argmax(axis=1)
print(classification_report(y_test, y_pred, target_names=["Loss", "Draw", "Win"]))

# -----------------------------------------------------------------------------------
# PyTorch
# -----------------------------------------------------------------------------------

# Making deterministic seeds:
torch.manual_seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

# Convert data to PyTorch tensors
X_train_tensor = torch.tensor(X_train, dtype=torch.float32)
y_train_tensor = torch.tensor(y_train, dtype=torch.long)

X_test_tensor = torch.tensor(X_test, dtype=torch.float32)
y_test_tensor = torch.tensor(y_test, dtype=torch.long)

# Wrap in DataLoader
train_dataset = TensorDataset(X_train_tensor, y_train_tensor)
train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)

test_dataset = TensorDataset(X_test_tensor, y_test_tensor)
test_loader = DataLoader(test_dataset, batch_size=16)

# Define model
class FootballNet(nn.Module):
    def __init__(self, input_size):
        super(FootballNet, self).__init__()
        self.fc1 = nn.Linear(input_size, 64)
        self.fc2 = nn.Linear(64, 32)
        self.out = nn.Linear(32, 3)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.out(x)

# Instantiate model
model = FootballNet(X_train.shape[1])
weights = torch.tensor([w0, w1, w2], dtype=torch.float32)
criterion = nn.CrossEntropyLoss(weight=weights)
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

# Train the model
epochs = 30
for epoch in range(epochs):
    model.train()
    running_loss = 0.0
    for X_batch, y_batch in train_loader:
        optimizer.zero_grad()
        outputs = model(X_batch)
        loss = criterion(outputs, y_batch)
        loss.backward()
        optimizer.step()
        running_loss += loss.item()

# Evaluate model accuracy
model.eval()
correct = 0
total = 0
all_preds = []
all_labels = []

with torch.no_grad():
    for X_batch, y_batch in test_loader:
        outputs = model(X_batch)
        _, predicted = torch.max(outputs, 1)
        all_preds.extend(predicted.numpy())
        all_labels.extend(y_batch.numpy())
        total += y_batch.size(0)
        correct += (predicted == y_batch).sum().item()

print(f"\nPyTorch Neural Network Approach: {correct / total:.4f}")

print("\nConfusion Matrix:\n", confusion_matrix(all_labels, all_preds))
print("\nClassification Report:\n", classification_report(all_labels, all_preds, target_names=["Loss", "Draw", "Win"]))

# Current Best (7/14/25) no added cols:
#     L   D   W 
# RF .51 .31 .53
# TF .49 .24 .47
# PT .44 .18 .46            .47 .20 .44 w/ 3 added cols         .49 .17 .48 w/o xg diff